from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import replace
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from tapeout_harvester.config import HarvesterConfig
from tapeout_harvester.engine import HarvesterEngine
from tapeout_harvester.models import NativeWithdrawal, Quote, SignedTx, TokenTransfer, TxReceipt
from tapeout_harvester.state import atomic_write_json, load_state, process_lock


WALLET = "0x1111111111111111111111111111111111111111"
REWARD = "0x3333333333333333333333333333333333333333"
DEST = "0x7777777777777777777777777777777777777777"
ROUTER = "0x4444444444444444444444444444444444444444"


def config(
    root: Path,
    *,
    sell_fraction="0",
    live_enabled=True,
    minimum_claim="1",
    timeout=120,
    unwrap_native=False,
    wallet=WALLET,
    max_gas="0.01",
    min_confirmations=1,
) -> HarvesterConfig:
    selling = Decimal(sell_fraction) > 0
    return HarvesterConfig(
        config_path=root / "config.toml",
        chain_id=56,
        rpc_urls=("https://example.invalid",),
        wallet=wallet,
        mining_contract="0x2222222222222222222222222222222222222222",
        reward_token=REWARD,
        reward_decimals=8,
        miner_keys=("0x" + "aa" * 32,),
        minimum_claim=Decimal(minimum_claim),
        sell_fraction=Decimal(sell_fraction),
        check_interval_seconds=60,
        router=ROUTER if selling else None,
        allowed_routers=(ROUTER,) if selling else (),
        quoter="0x5555555555555555555555555555555555555555" if selling else None,
        pool="0x6666666666666666666666666666666666666666" if selling else None,
        fee=2500 if selling else None,
        destination_token=DEST if selling else None,
        destination_decimals=18 if selling else None,
        unwrap_native=unwrap_native,
        wrapped_native_token=DEST if selling and unwrap_native else None,
        target_slippage_bps=25,
        hard_slippage_bps=75,
        quote_max_age_seconds=15,
        gas_reserve_native=Decimal("0.01"),
        max_gas_native_per_cycle=Decimal(max_gas),
        min_confirmations=min_confirmations,
        receipt_dir=root / "receipts",
        state_path=root / "state/state.json",
        lock_path=root / "state/lock",
        live_enabled=live_enabled,
        keystore_path=None,
        keyring_service=None,
        keyring_account=None,
        tx_receipt_timeout_seconds=timeout,
    )


class FakeAdapter:
    GAS_PRICE = 1_000_000_000

    def __init__(
        self,
        cfg: HarvesterConfig,
        *,
        pending=200_000_000,
        auto_mine=True,
        residual_allowance=False,
        swap_reverts=False,
    ):
        self.cfg = cfg
        self.pending = pending
        self.reward_balance = 0
        self.dest_balance = 0
        self.native_balance = 10**18
        self.allowance = 0
        self.auto_mine = auto_mine
        self.residual_allowance = residual_allowance
        self.swap_reverts = swap_reverts
        self.fail_swap_build = False
        self.deviation_bps = Decimal("5")
        self.quote_age = 0
        self.quote_out = 2 * 10**18
        self.confirmations = 10
        self.signed = 0
        self.broadcasts = 0
        self.sign_kinds: list[str] = []
        self.approve_builds = 0
        self.swap_builds = 0
        self._txs: dict[str, dict] = {}
        self._receipts: dict[str, TxReceipt] = {}
        self._applied: set[str] = set()
        self.gas_by_kind = {
            "claim": 100_000,
            "approve": 50_000,
            "swap": 200_000,
            "unwrap": 60_000,
        }

    def pending_raw(self): return self.pending
    def reward_balance_raw(self): return self.reward_balance
    def destination_balance_raw(self): return self.dest_balance
    def native_balance_wei(self): return self.native_balance
    def allowance_raw(self): return self.allowance

    def claim_tx(self):
        return {
            "kind": "claim",
            "claim_amount": self.pending,
            "gas": self.gas_by_kind["claim"],
            "gasPrice": self.GAS_PRICE,
        }

    def approve_tx(self, amount, *, gas_limit=None, gas_price_wei=None):
        self.approve_builds += 1
        return {
            "kind": "approve",
            "amount": int(amount),
            "gas": int(gas_limit or self.gas_by_kind["approve"]),
            "gasPrice": int(gas_price_wei or self.GAS_PRICE),
        }

    def swap_tx(self, quote):
        self.swap_builds += 1
        if self.fail_swap_build:
            raise RuntimeError("SYNTHETIC_SWAP_BUILD_FAILURE")
        if self.reward_balance < quote.amount_in_raw:
            raise RuntimeError("INSUFFICIENT_REWARD_BALANCE_FOR_SWAP_SIM")
        if self.allowance < quote.amount_in_raw:
            raise RuntimeError("INSUFFICIENT_ALLOWANCE_FOR_SWAP_SIM")
        return {
            "kind": "swap",
            "amount_in": quote.amount_in_raw,
            "amount_out": quote.amount_out_raw,
            "gas": self.gas_by_kind["swap"],
            "gasPrice": self.GAS_PRICE,
        }

    def unwrap_tx(self, amount):
        if self.dest_balance < int(amount):
            raise RuntimeError("INSUFFICIENT_WRAPPED_BALANCE_FOR_UNWRAP")
        return {
            "kind": "unwrap",
            "amount": int(amount),
            "gas": self.gas_by_kind["unwrap"],
            "gasPrice": self.GAS_PRICE,
        }

    def quote(self, amount):
        return Quote(
            amount_in_raw=int(amount),
            amount_out_raw=self.quote_out,
            min_out_raw=max(1, self.quote_out - 1),
            quoted_at=time.time() - self.quote_age,
            execution_price=Decimal("2"),
            spot_price=Decimal("2"),
            deviation_bps=self.deviation_bps,
        )

    def sign(self, tx, account):
        self.signed += 1
        kind = tx["kind"]
        self.sign_kinds.append(kind)
        tx_hash = "0x" + f"{self.signed:064x}"
        self._txs[tx_hash] = dict(tx)
        return SignedTx(tx_hash, f"raw-{self.signed}".encode())

    def broadcast(self, signed):
        self.broadcasts += 1
        if getattr(self, "broadcast_raises", False):
            raise OSError("transport lost after send")
        if self.auto_mine:
            status = 0 if self.swap_reverts and self._txs[signed.tx_hash]["kind"] == "swap" else 1
            self.mine(signed.tx_hash, status=status)
        return signed.tx_hash

    def _make_receipt(self, tx_hash: str, status: int) -> TxReceipt:
        tx = self._txs[tx_hash]
        transfers: list[TokenTransfer] = []
        withdrawals: list[NativeWithdrawal] = []
        if status == 1 and tx["kind"] == "claim":
            transfers.append(TokenTransfer(REWARD, "0x" + "00" * 20, self.cfg.wallet, int(tx["claim_amount"])))
        elif status == 1 and tx["kind"] == "swap":
            transfers.append(TokenTransfer(DEST, ROUTER, self.cfg.wallet, int(tx["amount_out"])))
        elif status == 1 and tx["kind"] == "unwrap":
            withdrawals.append(NativeWithdrawal(DEST, self.cfg.wallet, int(tx["amount"])))
        return TxReceipt(
            tx_hash,
            status,
            gas_used=int(tx.get("gas", 0) * 0.8),
            effective_gas_price=int(tx.get("gasPrice", self.GAS_PRICE)),
            block_number=100,
            block_hash="0x" + "ab" * 32,
            confirmations=self.confirmations,
            transfers=tuple(transfers),
            withdrawals=tuple(withdrawals),
        )

    def mine(self, tx_hash, status=1):
        self._receipts[tx_hash] = self._make_receipt(tx_hash, status)

    def set_confirmations(self, confirmations: int):
        self.confirmations = confirmations
        self._receipts = {
            h: replace(r, confirmations=confirmations)
            for h, r in self._receipts.items()
        }

    def get_receipt(self, tx_hash):
        receipt = self._receipts.get(tx_hash)
        if receipt and receipt.status == 1 and tx_hash not in self._applied:
            self._apply(self._txs[tx_hash])
            self._applied.add(tx_hash)
        return receipt

    def _apply(self, tx):
        if tx["kind"] == "claim":
            self.reward_balance += int(tx["claim_amount"])
            self.pending = max(0, self.pending - int(tx["claim_amount"]))
        elif tx["kind"] == "approve":
            self.allowance = int(tx["amount"])
        elif tx["kind"] == "swap":
            self.reward_balance -= int(tx["amount_in"])
            self.dest_balance += int(tx["amount_out"])
            if not self.residual_allowance:
                self.allowance = max(0, self.allowance - int(tx["amount_in"]))
        elif tx["kind"] == "unwrap":
            self.dest_balance -= int(tx["amount"])


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.signer_calls = 0

    def tearDown(self):
        self.tmp.cleanup()

    def signer(self):
        self.signer_calls += 1
        return SimpleNamespace(name="local-signer")

    def test_below_threshold_skips_without_signer(self):
        cfg = config(self.root, minimum_claim="1")
        a = FakeAdapter(cfg, pending=50_000_000)
        r = HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(r.status, "SKIP")
        self.assertEqual(self.signer_calls, 0)
        self.assertEqual(a.broadcasts, 0)

    def test_dry_run_never_builds_future_approve_or_swap(self):
        cfg = config(self.root, sell_fraction="1")
        a = FakeAdapter(cfg)
        r = HarvesterEngine(cfg, a, self.signer).run(live=False)
        self.assertEqual(r.status, "DRY_RUN_READY")
        self.assertFalse(r.signer_loaded)
        self.assertEqual(self.signer_calls, 0)
        self.assertEqual(a.approve_builds, 0)
        self.assertEqual(a.swap_builds, 0)
        self.assertFalse(r.details["post_claim_actions_simulated"])

    def test_live_requires_config_and_cli_switch(self):
        cfg = config(self.root, live_enabled=False)
        a = FakeAdapter(cfg)
        r = HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual((r.status, r.reason), ("BLOCKED", "CONFIG_LIVE_ENABLED_FALSE"))
        self.assertEqual(self.signer_calls, 0)

    def test_claim_only_completes_from_receipt_transfer(self):
        cfg = config(self.root)
        a = FakeAdapter(cfg)
        r = HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(r.claimed_raw, 200_000_000)
        self.assertEqual(r.sell_raw, 0)
        self.assertEqual(a.broadcasts, 1)
        data = json.loads((cfg.receipt_dir / r.details["receipt"]).read_text())
        self.assertEqual(data["claimed_raw"], 200_000_000)

    def test_zero_allowance_sell_executes_sequentially(self):
        cfg = config(self.root, sell_fraction="1")
        a = FakeAdapter(cfg)
        r = HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(a.sign_kinds, ["claim", "approve", "swap"])
        self.assertEqual(r.sell_raw, 200_000_000)
        self.assertEqual(a.allowance, 0)

    def test_non_exact_allowance_resets_then_exact_approve(self):
        cfg = config(self.root, sell_fraction="1")
        a = FakeAdapter(cfg)
        a.allowance = 123
        r = HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(a.sign_kinds, ["claim", "approve", "approve", "swap"])
        self.assertEqual(a.allowance, 0)

    def test_external_reward_deposit_not_counted_as_claimed(self):
        cfg = config(self.root, sell_fraction="1")
        a = FakeAdapter(cfg, auto_mine=False)
        e = HarvesterEngine(cfg, a, self.signer)
        first = e.run(live=True)
        self.assertEqual(first.status, "PENDING_TX")
        claim_hash = first.tx_hashes["claim"]
        a.mine(claim_hash)
        a.reward_balance += 900_000_000  # unrelated transfer while process is offline
        a.auto_mine = True
        result = e.run(live=True)
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(result.claimed_raw, 200_000_000)
        self.assertEqual(result.sell_raw, 200_000_000)
        self.assertEqual(a.reward_balance, 900_000_000)

    def test_inflight_state_rejects_wallet_or_policy_change(self):
        cfg_a = config(self.root)
        a = FakeAdapter(cfg_a, auto_mine=False)
        first = HarvesterEngine(cfg_a, a, self.signer).run(live=True)
        self.assertEqual(first.status, "PENDING_TX")
        cfg_b = config(
            self.root,
            wallet="0x8888888888888888888888888888888888888888",
            sell_fraction="1",
        )
        b = FakeAdapter(cfg_b)
        result = HarvesterEngine(cfg_b, b, self.signer).run(live=True)
        self.assertEqual((result.status, result.reason), ("BLOCKED", "INFLIGHT_CONFIG_IDENTITY_MISMATCH"))
        self.assertEqual(b.broadcasts, 0)

    def test_partial_sell_uses_receipt_claim_only(self):
        cfg = config(self.root, sell_fraction="0.5")
        a = FakeAdapter(cfg)
        a.reward_balance = 500_000_000
        r = HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(r.claimed_raw, 200_000_000)
        self.assertEqual(r.sell_raw, 100_000_000)
        self.assertEqual(a.reward_balance, 600_000_000)

    def test_swap_revert_triggers_persisted_cleanup(self):
        cfg = config(self.root, sell_fraction="1")
        a = FakeAdapter(cfg, swap_reverts=True)
        r = HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual((r.status, r.reason), ("BLOCKED_CLEANED", "SWAP_TX_REVERTED"))
        self.assertEqual(a.allowance, 0)
        self.assertEqual(a.sign_kinds, ["claim", "approve", "swap", "approve"])
        state = load_state(cfg.state_path)
        self.assertEqual(state["stage"], "BLOCKED_SAFE")

    def test_swap_build_failure_after_approval_cleans_allowance(self):
        cfg = config(self.root, sell_fraction="1")
        a = FakeAdapter(cfg)
        a.fail_swap_build = True
        r = HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual((r.status, r.reason), ("BLOCKED_CLEANED", "SWAP_PRE_BROADCAST_FAILED"))
        self.assertEqual(a.allowance, 0)
        self.assertEqual(a.sign_kinds, ["claim", "approve", "approve"])

    def test_residual_allowance_is_revoked_after_successful_swap(self):
        cfg = config(self.root, sell_fraction="1")
        a = FakeAdapter(cfg, residual_allowance=True)
        r = HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(r.status, "COMPLETE")
        self.assertEqual(a.sign_kinds, ["claim", "approve", "swap", "approve"])
        self.assertEqual(a.allowance, 0)

    def test_unwrap_uses_transaction_specific_withdrawal_event(self):
        cfg = config(self.root, sell_fraction="1", unwrap_native=True)
        a = FakeAdapter(cfg)
        r = HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(r.status, "COMPLETE")
        self.assertIn("unwrap", a.sign_kinds)
        self.assertEqual(a.dest_balance, 0)

    def test_pending_receipt_never_rebroadcasts(self):
        cfg = config(self.root)
        a = FakeAdapter(cfg, auto_mine=False)
        e = HarvesterEngine(cfg, a, self.signer)
        r1 = e.run(live=True)
        r2 = e.run(live=True)
        self.assertEqual(r1.status, "PENDING_TX")
        self.assertEqual(r2.status, "PENDING_TX")
        self.assertEqual(a.broadcasts, 1)
        a.mine(r1.tx_hashes["claim"])
        r3 = e.run(live=True)
        self.assertEqual(r3.status, "COMPLETE")
        self.assertEqual(a.broadcasts, 1)

    def test_confirmation_policy_waits_without_rebroadcast(self):
        cfg = config(self.root, min_confirmations=3)
        a = FakeAdapter(cfg)
        a.confirmations = 1
        e = HarvesterEngine(cfg, a, self.signer)
        r1 = e.run(live=True)
        self.assertEqual(r1.status, "PENDING_CONFIRMATIONS")
        self.assertEqual(a.broadcasts, 1)
        a.set_confirmations(3)
        r2 = e.run(live=True)
        self.assertEqual(r2.status, "COMPLETE")
        self.assertEqual(a.broadcasts, 1)

    def test_broadcast_exception_is_unknown_and_not_retried(self):
        cfg = config(self.root)
        a = FakeAdapter(cfg, auto_mine=False)
        a.broadcast_raises = True
        e = HarvesterEngine(cfg, a, self.signer)
        r1 = e.run(live=True)
        self.assertEqual((r1.status, r1.reason), ("BLOCKED_UNKNOWN", "BROADCAST_RESULT_UNKNOWN"))
        self.assertEqual(a.broadcasts, 1)
        a.broadcast_raises = False
        r2 = e.run(live=True)
        self.assertEqual(r2.status, "PENDING_TX")
        self.assertEqual(a.broadcasts, 1)

    def test_receipt_timeout_is_unknown_without_rebroadcast(self):
        cfg = config(self.root, timeout=1)
        a = FakeAdapter(cfg, auto_mine=False)
        e = HarvesterEngine(cfg, a, self.signer)
        r1 = e.run(live=True)
        st = load_state(cfg.state_path)
        st["tx_intent_at"]["claim"] = time.time() - 5
        atomic_write_json(cfg.state_path, st)
        r2 = e.run(live=True)
        self.assertEqual((r2.status, r2.reason), ("BLOCKED_UNKNOWN", "RECEIPT_TIMEOUT_NO_REBROADCAST"))
        self.assertEqual(a.broadcasts, 1)

    def test_claim_revert_blocks_without_sell(self):
        cfg = config(self.root)
        a = FakeAdapter(cfg, auto_mine=False)
        e = HarvesterEngine(cfg, a, self.signer)
        r1 = e.run(live=True)
        a.mine(r1.tx_hashes["claim"], status=0)
        r2 = e.run(live=True)
        self.assertEqual((r2.status, r2.reason), ("BLOCKED", "CLAIM_TX_REVERTED"))
        self.assertEqual(a.broadcasts, 1)

    def test_stale_preview_quote_fails_before_signer(self):
        cfg = config(self.root, sell_fraction="1")
        a = FakeAdapter(cfg)
        a.quote_age = 100
        with self.assertRaisesRegex(RuntimeError, "QUOTE_STALE"):
            HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(self.signer_calls, 0)
        self.assertEqual(a.broadcasts, 0)

    def test_hard_quote_deviation_fails_before_signer(self):
        cfg = config(self.root, sell_fraction="1")
        a = FakeAdapter(cfg)
        a.deviation_bps = Decimal("100")
        with self.assertRaisesRegex(RuntimeError, "QUOTE_DEVIATION"):
            HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(self.signer_calls, 0)

    def test_cumulative_gas_cap_stops_before_swap_and_cleans(self):
        cfg = config(self.root, sell_fraction="1", max_gas="0.0004")
        a = FakeAdapter(cfg)
        r = HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(r.status, "BLOCKED_CLEANED")
        self.assertEqual(r.reason, "SWAP_PRE_BROADCAST_FAILED")
        self.assertNotIn("swap", a.sign_kinds)
        self.assertEqual(a.allowance, 0)
        state = load_state(cfg.state_path)
        self.assertLessEqual(int(state["gas_committed_wei"]), int(Decimal("0.0004") * Decimal(10**18)))

    def test_gas_cap_fails_before_signer_on_claim(self):
        cfg = config(self.root, max_gas="0.00005")
        a = FakeAdapter(cfg)
        with self.assertRaisesRegex(RuntimeError, "MAX_GAS"):
            HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(self.signer_calls, 0)

    def test_gas_reserve_fails_before_signer(self):
        cfg = config(self.root)
        a = FakeAdapter(cfg)
        a.native_balance = 10**15
        with self.assertRaisesRegex(RuntimeError, "GAS_RESERVE"):
            HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(self.signer_calls, 0)

    def test_wallet_lock_is_stable_across_config_directories(self):
        other = Path(tempfile.mkdtemp())
        try:
            self.assertEqual(config(self.root).wallet_lock_path, config(other).wallet_lock_path)
        finally:
            other.rmdir()


    def test_receipt_rpc_failure_after_swap_keeps_inflight_and_reconciles_later(self):
        cfg = config(self.root, sell_fraction="1")

        class ReceiptFaultAdapter(FakeAdapter):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.fail_swap_receipt = True

            def get_receipt(self, tx_hash):
                tx = self._txs.get(tx_hash)
                if (
                    self.fail_swap_receipt
                    and tx
                    and tx.get("kind") == "swap"
                    and tx_hash in self._receipts
                ):
                    raise OSError("synthetic receipt transport failure")
                return super().get_receipt(tx_hash)

        a = ReceiptFaultAdapter(cfg)
        e = HarvesterEngine(cfg, a, self.signer)
        first = e.run(live=True)
        self.assertEqual(
            (first.status, first.reason),
            ("BLOCKED_UNKNOWN", "RECEIPT_QUERY_FAILED_NO_REBROADCAST"),
        )
        self.assertEqual(a.broadcasts, 3)
        self.assertEqual(a.allowance, 200_000_000)
        state = load_state(cfg.state_path)
        self.assertEqual(state["stage"], "SWAP_INTENT")
        self.assertIn("swap", state["tx_hashes"])

        a.fail_swap_receipt = False
        second = e.run(live=True)
        self.assertEqual(second.status, "COMPLETE")
        self.assertEqual(a.broadcasts, 3)
        self.assertEqual(a.allowance, 0)

    def test_confirmed_approval_mismatch_uses_reserved_cleanup(self):
        cfg = config(self.root, sell_fraction="1")

        class ApprovalMismatchAdapter(FakeAdapter):
            def __init__(self, cfg):
                super().__init__(cfg)
                self.injected = False

            def get_receipt(self, tx_hash):
                receipt = super().get_receipt(tx_hash)
                tx = self._txs.get(tx_hash)
                if (
                    receipt
                    and not self.injected
                    and tx
                    and tx.get("kind") == "approve"
                    and int(tx.get("amount", 0)) > 0
                ):
                    self.allowance = int(tx["amount"]) - 1
                    self.injected = True
                return receipt

        a = ApprovalMismatchAdapter(cfg)
        result = HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(
            (result.status, result.reason),
            ("BLOCKED_CLEANED", "EXACT_ALLOWANCE_MISMATCH"),
        )
        self.assertEqual(a.allowance, 0)
        self.assertNotIn("swap", a.sign_kinds)
        self.assertEqual(a.sign_kinds, ["claim", "approve", "approve"])

    def test_quote_expiring_after_sign_is_not_broadcast_and_allowance_is_cleaned(self):
        cfg = config(self.root, sell_fraction="1")
        clock = [1_000.0]

        class SlowSignAdapter(FakeAdapter):
            def quote(self, amount):
                return Quote(
                    amount_in_raw=int(amount),
                    amount_out_raw=self.quote_out,
                    min_out_raw=max(1, self.quote_out - 1),
                    quoted_at=clock[0],
                    execution_price=Decimal("2"),
                    spot_price=Decimal("2"),
                    deviation_bps=Decimal("5"),
                )

            def sign(self, tx, account):
                signed = super().sign(tx, account)
                if tx["kind"] == "swap":
                    clock[0] += self.cfg.quote_max_age_seconds + 5
                return signed

        a = SlowSignAdapter(cfg)
        with patch("tapeout_harvester.engine.time.time", side_effect=lambda: clock[0]):
            result = HarvesterEngine(cfg, a, self.signer).run(live=True)

        self.assertEqual(
            (result.status, result.reason),
            ("BLOCKED_CLEANED", "SWAP_PRE_BROADCAST_FAILED"),
        )
        self.assertEqual(a.allowance, 0)
        self.assertEqual(a.broadcasts, 3)  # claim + exact approve + cleanup; swap never broadcast
        state = load_state(cfg.state_path)
        self.assertIn("swap", state["not_broadcast_hashes"])

    def test_claim_receipt_reorg_before_swap_blocks_and_cleans(self):
        cfg = config(self.root, sell_fraction="1")

        class ReorgAfterApprovalAdapter(FakeAdapter):
            def get_receipt(self, tx_hash):
                receipt = super().get_receipt(tx_hash)
                tx = self._txs.get(tx_hash)
                if (
                    receipt
                    and tx
                    and tx.get("kind") == "claim"
                    and self.allowance > 0
                ):
                    return replace(
                        receipt,
                        block_hash="0x" + "cd" * 32,
                        transfers=(
                            TokenTransfer(
                                REWARD,
                                "0x" + "00" * 20,
                                self.cfg.wallet,
                                100_000_000,
                            ),
                        ),
                    )
                return receipt

        a = ReorgAfterApprovalAdapter(cfg)
        result = HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(
            (result.status, result.reason),
            ("BLOCKED_CLEANED", "POST_APPROVAL_SELL_PREP_FAILED"),
        )
        self.assertEqual(a.allowance, 0)
        self.assertNotIn("swap", a.sign_kinds)

    def test_signed_not_broadcast_crash_recovers_cleanup_after_restart(self):
        cfg = config(self.root, sell_fraction="1")
        clock = [1_000.0]

        class ProcessDeath(BaseException):
            pass

        class SlowSignAdapter(FakeAdapter):
            def quote(self, amount):
                return Quote(
                    amount_in_raw=int(amount),
                    amount_out_raw=self.quote_out,
                    min_out_raw=max(1, self.quote_out - 1),
                    quoted_at=clock[0],
                    execution_price=Decimal("2"),
                    spot_price=Decimal("2"),
                    deviation_bps=Decimal("5"),
                )

            def sign(self, tx, account):
                signed = super().sign(tx, account)
                if tx["kind"] == "swap":
                    clock[0] += self.cfg.quote_max_age_seconds + 5
                return signed

        class CrashAfterJournal(HarvesterEngine):
            def _save(self, state):
                super()._save(state)
                if state.get("stage") == "SIGNED_NOT_BROADCAST":
                    raise ProcessDeath()

        a = SlowSignAdapter(cfg)
        with patch("tapeout_harvester.engine.time.time", side_effect=lambda: clock[0]):
            with self.assertRaises(ProcessDeath):
                CrashAfterJournal(cfg, a, self.signer).run(live=True)

        persisted = load_state(cfg.state_path)
        self.assertEqual(persisted["stage"], "SIGNED_NOT_BROADCAST")
        self.assertEqual(persisted["not_broadcast_label"], "swap")
        self.assertTrue(persisted["approval_created_by_cycle"])
        self.assertEqual(a.allowance, 200_000_000)

        with patch("tapeout_harvester.engine.time.time", side_effect=lambda: clock[0]):
            resumed = HarvesterEngine(cfg, a, self.signer).run(live=True)
        self.assertEqual(
            (resumed.status, resumed.reason),
            ("BLOCKED_CLEANED", "SWAP_PRE_BROADCAST_FAILED"),
        )
        self.assertEqual(a.allowance, 0)
        self.assertEqual(a.broadcasts, 3)  # claim + exact approve + cleanup; signed swap was never broadcast
        self.assertEqual(a.sign_kinds.count("swap"), 1)

    def test_slow_claim_receipt_revalidation_expires_quote_before_swap_sign(self):
        cfg = config(self.root, sell_fraction="1")
        clock = [2_000.0]

        class SlowReceiptAdapter(FakeAdapter):
            def quote(self, amount):
                return Quote(
                    amount_in_raw=int(amount),
                    amount_out_raw=self.quote_out,
                    min_out_raw=max(1, self.quote_out - 1),
                    quoted_at=clock[0],
                    execution_price=Decimal("2"),
                    spot_price=Decimal("2"),
                    deviation_bps=Decimal("5"),
                )

            def get_receipt(self, tx_hash):
                receipt = super().get_receipt(tx_hash)
                tx = self._txs.get(tx_hash)
                if (
                    receipt
                    and tx
                    and tx.get("kind") == "claim"
                    and self.allowance > 0
                ):
                    clock[0] += self.cfg.quote_max_age_seconds + 5
                return receipt

        a = SlowReceiptAdapter(cfg)
        with patch("tapeout_harvester.engine.time.time", side_effect=lambda: clock[0]):
            result = HarvesterEngine(cfg, a, self.signer).run(live=True)

        self.assertEqual(
            (result.status, result.reason),
            ("BLOCKED_CLEANED", "SWAP_PRE_BROADCAST_FAILED"),
        )
        self.assertNotIn("swap", a.sign_kinds)
        self.assertEqual(a.allowance, 0)

    def test_swap_reorg_during_unwrap_build_prevents_unwrap_broadcast(self):
        cfg = config(self.root, sell_fraction="1", unwrap_native=True)

        class ReorgDuringUnwrapAdapter(FakeAdapter):
            def unwrap_tx(self, amount):
                tx = super().unwrap_tx(amount)
                for tx_hash, built in self._txs.items():
                    if built.get("kind") == "swap" and tx_hash in self._receipts:
                        self._receipts[tx_hash] = replace(
                            self._receipts[tx_hash],
                            block_hash="0x" + ("cd" * 32),
                            transfers=(
                                TokenTransfer(
                                    DEST,
                                    ROUTER,
                                    self.cfg.wallet,
                                    int(amount) - 1,
                                ),
                            ),
                        )
                return tx

        a = ReorgDuringUnwrapAdapter(cfg)
        a.dest_balance = 1_000_000_000_000_000_000
        result = HarvesterEngine(cfg, a, self.signer).run(live=True)

        self.assertEqual((result.status, result.reason), ("BLOCKED", "UNWRAP_PREP_FAILED"))
        self.assertNotIn("unwrap", a.sign_kinds)
        self.assertEqual(a.dest_balance, 3_000_000_000_000_000_000)

    def test_claim_uses_net_receipt_flow_not_gross_incoming_transfer(self):
        cfg = config(self.root, sell_fraction="1")

        class FeeTransferAdapter(FakeAdapter):
            def _make_receipt(self, tx_hash, status):
                receipt = super()._make_receipt(tx_hash, status)
                tx = self._txs[tx_hash]
                if status == 1 and tx.get("kind") == "claim":
                    receipt = replace(
                        receipt,
                        transfers=receipt.transfers + (
                            TokenTransfer(
                                REWARD,
                                self.cfg.wallet,
                                "0x" + ("99" * 20),
                                20_000_000,
                            ),
                        ),
                    )
                return receipt

            def _apply(self, tx):
                super()._apply(tx)
                if tx.get("kind") == "claim":
                    self.reward_balance -= 20_000_000

        a = FeeTransferAdapter(cfg)
        a.reward_balance = 500_000_000
        result = HarvesterEngine(cfg, a, self.signer).run(live=True)

        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(result.claimed_raw, 180_000_000)
        self.assertEqual(result.sell_raw, 180_000_000)
        self.assertEqual(a.reward_balance, 500_000_000)

    def test_process_lock_prevents_second_instance(self):
        cfg = config(self.root)
        with process_lock(cfg.lock_path):
            with self.assertRaisesRegex(RuntimeError, "LOCK_HELD"):
                with process_lock(cfg.lock_path):
                    pass


if __name__ == "__main__":
    unittest.main()
