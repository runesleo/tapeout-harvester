import json
import tempfile
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import patch

from tapeout_harvester.cli import (
    _doctor,
    _heartbeat_status_for_result,
    _write_heartbeat,
)
from tapeout_harvester.config import HarvesterConfig
from tapeout_harvester.models import NativeWithdrawal, TokenTransfer, TxReceipt


MINER_KEY = "0x" + "aa" * 32

BASE = f"""
[network]
chain_id = 56
rpc_urls = ["https://example.invalid"]
wallet = "0x1111111111111111111111111111111111111111"
[contracts]
mining = "0x2222222222222222222222222222222222222222"
reward_token = "0x3333333333333333333333333333333333333333"
reward_decimals = 8
[mining]
miner_keys = ["{MINER_KEY}"]
[harvest]
minimum_claim = "1"
sell_fraction = "0"
check_interval_seconds = 60
[safety]
target_slippage_bps = 25
hard_slippage_bps = 75
quote_max_age_seconds = 15
gas_reserve_native = "0.01"
max_gas_native_per_cycle = "0.001"
tx_receipt_timeout_seconds = 120
min_confirmations = 1
[runtime]
live_enabled = false
"""


class FakeAdapter:
    def __init__(self, _cfg):
        self.rpc_label = "https://rpc.example"

    def pending_raw(self):
        return 123

    def get_receipt(self, _tx_hash):
        return None

    def allowance_raw(self):
        return 0

    def reward_balance_raw(self):
        return 10**30

    def destination_balance_raw(self):
        return 10**30


class CliHealthTests(unittest.TestCase):
    def make_cfg(self, td: str) -> HarvesterConfig:
        p = Path(td) / "config.toml"
        p.write_text(BASE, encoding="utf-8")
        return HarvesterConfig.load(p)

    def make_sell_cfg(self, td: str, *, unwrap_native: bool = False) -> HarvesterConfig:
        route = """
router = "0x4444444444444444444444444444444444444444"
allowed_routers = ["0x4444444444444444444444444444444444444444"]
quoter = "0x5555555555555555555555555555555555555555"
pool = "0x6666666666666666666666666666666666666666"
fee = 2500
"""
        destination = f"""
[destination]
token = "0x7777777777777777777777777777777777777777"
decimals = 18
unwrap_native = {"true" if unwrap_native else "false"}
wrapped_native_token = "0x7777777777777777777777777777777777777777"
"""
        text = (
            BASE.replace(
                "reward_decimals = 8\n",
                "reward_decimals = 8\n" + route,
            )
            .replace('sell_fraction = "0"', 'sell_fraction = "1"')
            + destination
        )
        p = Path(td) / "config.toml"
        p.write_text(text, encoding="utf-8")
        return HarvesterConfig.load(p)

    @staticmethod
    def inflight(cfg: HarvesterConfig, stage: str, label: str) -> dict:
        return {
            "schema": "tapeout-harvester-state/v2",
            "stage": stage,
            "cycle_identity_hash": cfg.cycle_identity_hash(),
            "cycle_identity": cfg.cycle_identity(),
            "tx_hashes": {label: "0x" + "ab" * 32},
            "tx_intent_at": {label: time.time()},
        }

    def test_heartbeat_is_written_without_secret_runtime_detail(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            adapter = SimpleNamespace(rpc_label="https://rpc.example")
            _write_heartbeat(
                cfg,
                status="ERROR",
                live=False,
                adapter=adapter,
                error=RuntimeError("provider leaked a very long sensitive-looking runtime detail"),
            )
            payload = json.loads(cfg.heartbeat_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "tapeout-harvester-heartbeat/v1")
            self.assertEqual(payload["status"], "ERROR")
            self.assertEqual(
                payload["cycle_identity_hash"],
                cfg.cycle_identity_hash(),
            )
            self.assertEqual(
                payload["watcher_identity_hash"],
                cfg.watcher_identity_hash(),
            )
            self.assertEqual(payload["rpc"], "https://rpc.example")
            self.assertEqual(payload["message"], "RUNTIME_DETAIL_REDACTED")

    def test_heartbeat_uses_dedicated_blocking_lock(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            with patch("tapeout_harvester.cli.process_lock", return_value=nullcontext()) as lock:
                with patch("tapeout_harvester.cli.atomic_write_json"):
                    _write_heartbeat(cfg, status="OK", live=False)
            lock.assert_called_once_with(cfg.heartbeat_lock_path, blocking=True)

    def test_blocked_results_map_to_degraded_heartbeat(self):
        for result_status in ("BLOCKED", "BLOCKED_UNKNOWN", "BLOCKED_CLEANED"):
            self.assertEqual(_heartbeat_status_for_result(result_status), "DEGRADED")
        for result_status in ("SKIP", "DRY_RUN_READY", "COMPLETE", "PENDING_TX", "PENDING_CONFIRMATIONS"):
            self.assertEqual(_heartbeat_status_for_result(result_status), "OK")

    def test_doctor_without_heartbeat_is_ok_but_watch_not_observed(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            with patch("tapeout_harvester.cli.Web3TapeOutAdapter", FakeAdapter):
                report = _doctor(cfg)
            self.assertEqual(report["status"], "OK")
            self.assertEqual(report["watch_status"], "NOT_OBSERVED")
            self.assertFalse(report["inflight"])
            self.assertEqual(report["pending_raw"], 123)

    def test_doctor_degrades_fresh_heartbeat_from_different_config(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            old = replace(cfg, minimum_claim=Decimal("2"))
            old.validate()
            cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.heartbeat_path.write_text(
                json.dumps(
                    {
                        "schema": "tapeout-harvester-heartbeat/v1",
                        "updated_at": time.time(),
                        "status": "OK",
                        "result_status": "SKIP",
                        "cycle_identity_hash": old.cycle_identity_hash(),
                        "watcher_identity_hash": old.watcher_identity_hash(),
                    }
                ),
                encoding="utf-8",
            )
            with patch("tapeout_harvester.cli.Web3TapeOutAdapter", FakeAdapter):
                report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(report["watch_status"], "DEGRADED")

    def test_doctor_marks_stale_heartbeat_degraded(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.heartbeat_path.write_text(
                json.dumps(
                    {
                        "schema": "tapeout-harvester-heartbeat/v1",
                        "updated_at": time.time() - 600,
                        "status": "OK",
                    }
                ),
                encoding="utf-8",
            )
            with patch("tapeout_harvester.cli.Web3TapeOutAdapter", FakeAdapter):
                report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(report["watch_status"], "STALE")
            self.assertGreater(report["heartbeat_age_seconds"], 500)

    def test_doctor_treats_complete_as_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            completed = {
                "schema": "tapeout-harvester-state/v2",
                "stage": "COMPLETE",
                "last_completed_at": 123.0,
            }
            with patch("tapeout_harvester.cli.load_state", return_value=completed):
                with patch("tapeout_harvester.cli.Web3TapeOutAdapter", FakeAdapter):
                    report = _doctor(cfg)
            self.assertFalse(report["inflight"])
            self.assertEqual(report["state_stage"], "COMPLETE")

    def test_doctor_degrades_persisted_blocked_safe_without_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            blocked = {
                "schema": "tapeout-harvester-state/v2",
                "stage": "BLOCKED_SAFE",
                "failure_reason": "CLEANUP_REVOKE_TX_REVERTED",
            }
            with patch("tapeout_harvester.cli.load_state", return_value=blocked):
                with patch("tapeout_harvester.cli.Web3TapeOutAdapter", FakeAdapter):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(report["state_health"], "DEGRADED")
            self.assertEqual(report["state_failure_reason"], "CLEANUP_REVOKE_TX_REVERTED")
            self.assertEqual(report["watch_status"], "NOT_OBSERVED")

    def test_doctor_degrades_persisted_blocked_safe_despite_healthy_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            blocked = {
                "schema": "tapeout-harvester-state/v2",
                "stage": "BLOCKED_SAFE",
                "failure_reason": "CLEANUP_REVOKE_TX_REVERTED",
            }
            cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.heartbeat_path.write_text(
                json.dumps(
                    {
                        "schema": "tapeout-harvester-heartbeat/v1",
                        "updated_at": time.time(),
                        "status": "OK",
                        "result_status": "SKIP",
                        "cycle_identity_hash": cfg.cycle_identity_hash(),
                        "watcher_identity_hash": cfg.watcher_identity_hash(),
                    }
                ),
                encoding="utf-8",
            )
            with patch("tapeout_harvester.cli.load_state", return_value=blocked):
                with patch("tapeout_harvester.cli.Web3TapeOutAdapter", FakeAdapter):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(report["state_health"], "DEGRADED")
            self.assertEqual(report["watch_status"], "OK")

    def test_doctor_degrades_inflight_config_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            inflight = {
                "schema": "tapeout-harvester-state/v2",
                "stage": "CLAIM_INTENT",
                "cycle_identity_hash": cfg.cycle_identity_hash(),
                "cycle_identity": cfg.cycle_identity(),
                "tx_hashes": {"claim": "0x" + "ab" * 32},
                "tx_intent_at": {"claim": time.time()},
            }
            changed = replace(cfg, minimum_claim=Decimal("2"))
            changed.validate()
            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch("tapeout_harvester.cli.Web3TapeOutAdapter", FakeAdapter):
                    report = _doctor(changed)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(report["state_health"], "DEGRADED")
            self.assertEqual(report["state_failure_reason"], "INFLIGHT_CONFIG_IDENTITY_MISMATCH")
            self.assertTrue(report["inflight"])

    def test_doctor_keeps_matching_inflight_identity_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            inflight = {
                "schema": "tapeout-harvester-state/v2",
                "stage": "CLAIM_INTENT",
                "cycle_identity_hash": cfg.cycle_identity_hash(),
                "cycle_identity": cfg.cycle_identity(),
                "tx_hashes": {"claim": "0x" + "ab" * 32},
                "tx_intent_at": {"claim": time.time()},
            }
            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch("tapeout_harvester.cli.Web3TapeOutAdapter", FakeAdapter):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "OK")
            self.assertEqual(report["state_health"], "OK")
            self.assertIsNone(report["state_failure_reason"])
            self.assertTrue(report["inflight"])

    def test_doctor_degrades_timed_out_inflight_without_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            inflight = {
                "schema": "tapeout-harvester-state/v2",
                "stage": "CLAIM_INTENT",
                "cycle_identity_hash": cfg.cycle_identity_hash(),
                "cycle_identity": cfg.cycle_identity(),
                "tx_hashes": {"claim": "0x" + "ab" * 32},
                "tx_intent_at": {"claim": time.time() - 121},
            }
            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch("tapeout_harvester.cli.Web3TapeOutAdapter", FakeAdapter):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(report["state_health"], "DEGRADED")
            self.assertEqual(
                report["state_failure_reason"],
                "RECEIPT_TIMEOUT_NO_REBROADCAST",
            )

    def test_doctor_degrades_receipt_query_failure(self):
        class FailingReceiptAdapter(FakeAdapter):
            def get_receipt(self, _tx_hash):
                raise OSError("rpc unavailable")

        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            inflight = {
                "schema": "tapeout-harvester-state/v2",
                "stage": "CLAIM_INTENT",
                "cycle_identity_hash": cfg.cycle_identity_hash(),
                "cycle_identity": cfg.cycle_identity(),
                "tx_hashes": {"claim": "0x" + "ab" * 32},
                "tx_intent_at": {"claim": time.time()},
            }
            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    FailingReceiptAdapter,
                ):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(
                report["state_failure_reason"],
                "RECEIPT_QUERY_FAILED_NO_REBROADCAST",
            )

    def test_doctor_degrades_confirmed_reverted_inflight_receipt(self):
        class RevertedReceiptAdapter(FakeAdapter):
            def get_receipt(self, _tx_hash):
                return SimpleNamespace(status=0, confirmations=1)

        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            inflight = {
                "schema": "tapeout-harvester-state/v2",
                "stage": "CLAIM_INTENT",
                "cycle_identity_hash": cfg.cycle_identity_hash(),
                "cycle_identity": cfg.cycle_identity(),
                "tx_hashes": {"claim": "0x" + "ab" * 32},
                "tx_intent_at": {"claim": time.time()},
            }
            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    RevertedReceiptAdapter,
                ):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(report["state_failure_reason"], "CLAIM_TX_REVERTED")

    def test_doctor_degrades_successful_claim_without_attributed_reward(self):
        class EmptyClaimReceiptAdapter(FakeAdapter):
            def get_receipt(self, tx_hash):
                return TxReceipt(
                    tx_hash=tx_hash,
                    status=1,
                    confirmations=1,
                )

        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            inflight = self.inflight(cfg, "CLAIM_INTENT", "claim")
            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    EmptyClaimReceiptAdapter,
                ):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(
                report["state_failure_reason"],
                "CLAIM_RECEIPT_HAS_NO_ATTRIBUTED_REWARD_TRANSFER",
            )

    def test_doctor_accepts_successful_claim_with_attributed_reward(self):
        wallet = "0x1111111111111111111111111111111111111111"
        reward = "0x3333333333333333333333333333333333333333"

        class AttributedClaimReceiptAdapter(FakeAdapter):
            def get_receipt(self, tx_hash):
                return TxReceipt(
                    tx_hash=tx_hash,
                    status=1,
                    confirmations=1,
                    transfers=(
                        TokenTransfer(
                            reward,
                            "0x0000000000000000000000000000000000000000",
                            wallet,
                            123,
                        ),
                    ),
                )

        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            inflight = self.inflight(cfg, "CLAIM_INTENT", "claim")
            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    AttributedClaimReceiptAdapter,
                ):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "OK")
            self.assertEqual(report["state_health"], "OK")
            self.assertIsNone(report["state_failure_reason"])

    def test_doctor_degrades_successful_approve_reset_when_allowance_remains(self):
        class ResetMismatchAdapter(FakeAdapter):
            def get_receipt(self, tx_hash):
                return TxReceipt(tx_hash=tx_hash, status=1, confirmations=1)

            def allowance_raw(self):
                return 1

        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_sell_cfg(td)
            inflight = self.inflight(
                cfg,
                "APPROVE_RESET_INTENT",
                "approve_reset",
            )
            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    ResetMismatchAdapter,
                ):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(report["state_failure_reason"], "ALLOWANCE_RESET_FAILED")

    def test_doctor_degrades_successful_swap_without_attributed_destination(self):
        class EmptySwapReceiptAdapter(FakeAdapter):
            def get_receipt(self, tx_hash):
                return TxReceipt(tx_hash=tx_hash, status=1, confirmations=1)

        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_sell_cfg(td)
            inflight = self.inflight(cfg, "SWAP_INTENT", "swap")
            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    EmptySwapReceiptAdapter,
                ):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(
                report["state_failure_reason"],
                "SWAP_RECEIPT_HAS_NO_ATTRIBUTED_DESTINATION_TRANSFER",
            )

    def test_doctor_degrades_successful_unwrap_with_wrong_event_amount(self):
        wrapped = "0x7777777777777777777777777777777777777777"
        wallet = "0x1111111111111111111111111111111111111111"

        class WrongUnwrapReceiptAdapter(FakeAdapter):
            def get_receipt(self, tx_hash):
                return TxReceipt(
                    tx_hash=tx_hash,
                    status=1,
                    confirmations=1,
                    withdrawals=(NativeWithdrawal(wrapped, wallet, 4),),
                )

        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_sell_cfg(td, unwrap_native=True)
            inflight = self.inflight(cfg, "UNWRAP_INTENT", "unwrap")
            inflight["received_raw"] = 5
            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    WrongUnwrapReceiptAdapter,
                ):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(
                report["state_failure_reason"],
                "UNWRAP_RECEIPT_EVENT_MISMATCH",
            )

    def test_doctor_degrades_successful_cleanup_that_will_terminally_block(self):
        class CleanRevokeReceiptAdapter(FakeAdapter):
            def get_receipt(self, tx_hash):
                return TxReceipt(tx_hash=tx_hash, status=1, confirmations=1)

        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_sell_cfg(td)
            inflight = self.inflight(
                cfg,
                "CLEANUP_REVOKE_INTENT",
                "cleanup_revoke",
            )
            inflight["cleanup_continue"] = "BLOCK"
            inflight["cleanup_reason"] = "POST_APPROVAL_SELL_PREP_FAILED"
            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    CleanRevokeReceiptAdapter,
                ):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(
                report["state_failure_reason"],
                "POST_APPROVAL_SELL_PREP_FAILED",
            )

    def test_doctor_retries_when_journal_changes_during_diagnosis(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_sell_cfg(td)
            inflight = self.inflight(cfg, "APPROVE_INTENT", "approve")
            inflight["sell_raw"] = 100
            complete = {
                "schema": "tapeout-harvester-state/v2",
                "stage": "COMPLETE",
                "last_completed_at": 123.0,
            }
            states = [inflight, complete, complete, complete]

            def read_state(_path):
                return states.pop(0) if states else complete

            class ExactAllowanceAdapter(FakeAdapter):
                def get_receipt(self, tx_hash):
                    return TxReceipt(tx_hash=tx_hash, status=1, confirmations=1)

                def allowance_raw(self):
                    return 0

            with patch("tapeout_harvester.cli.load_state", side_effect=read_state):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    ExactAllowanceAdapter,
                ):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "OK")
            self.assertEqual(report["state_stage"], "COMPLETE")
            self.assertFalse(report["inflight"])
            self.assertIsNone(report["state_failure_reason"])

    def test_doctor_degrades_approve_when_accepted_claim_receipt_changed(self):
        wallet = "0x1111111111111111111111111111111111111111"
        reward = "0x3333333333333333333333333333333333333333"

        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_sell_cfg(td)
            inflight = self.inflight(cfg, "APPROVE_INTENT", "approve")
            inflight["sell_raw"] = 100
            inflight["accepted_receipts"] = {
                "claim": {
                    "tx_hash": "0xclaim",
                    "block_number": 10,
                    "block_hash": "0xold",
                    "token": reward,
                    "recipient": wallet,
                    "amount_raw": 100,
                }
            }

            class ChangedClaimReceiptAdapter(FakeAdapter):
                def get_receipt(self, tx_hash):
                    if tx_hash == "0xclaim":
                        return TxReceipt(
                            tx_hash=tx_hash,
                            status=1,
                            confirmations=1,
                            block_number=10,
                            block_hash="0xnew",
                            transfers=(
                                TokenTransfer(
                                    reward,
                                    "0x0000000000000000000000000000000000000000",
                                    wallet,
                                    100,
                                ),
                            ),
                        )
                    return TxReceipt(
                        tx_hash=tx_hash,
                        status=1,
                        confirmations=1,
                    )

                def allowance_raw(self):
                    return 100

            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    ChangedClaimReceiptAdapter,
                ):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(
                report["state_failure_reason"],
                "ACCEPTED_RECEIPT_BLOCK_CHANGED",
            )

    def test_doctor_degrades_successful_swap_with_residual_allowance(self):
        wallet = "0x1111111111111111111111111111111111111111"
        destination = "0x7777777777777777777777777777777777777777"

        class ResidualAllowanceAdapter(FakeAdapter):
            def get_receipt(self, tx_hash):
                return TxReceipt(
                    tx_hash=tx_hash,
                    status=1,
                    confirmations=1,
                    transfers=(
                        TokenTransfer(
                            destination,
                            "0x4444444444444444444444444444444444444444",
                            wallet,
                            50,
                        ),
                    ),
                )

            def allowance_raw(self):
                return 1

        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_sell_cfg(td)
            inflight = self.inflight(cfg, "SWAP_INTENT", "swap")
            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    ResidualAllowanceAdapter,
                ):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(
                report["state_failure_reason"],
                "POST_SWAP_RESIDUAL_ALLOWANCE",
            )

    def test_doctor_degrades_post_swap_cleanup_when_accepted_swap_changed(self):
        wallet = "0x1111111111111111111111111111111111111111"
        destination = "0x7777777777777777777777777777777777777777"

        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_sell_cfg(td, unwrap_native=True)
            inflight = self.inflight(
                cfg,
                "CLEANUP_REVOKE_INTENT",
                "cleanup_revoke",
            )
            inflight["cleanup_continue"] = "POST_SWAP"
            inflight["cleanup_reason"] = "POST_SWAP_RESIDUAL_ALLOWANCE"
            inflight["received_raw"] = 50
            inflight["accepted_receipts"] = {
                "swap": {
                    "tx_hash": "0xswap",
                    "block_number": 10,
                    "block_hash": "0xold",
                    "token": destination,
                    "recipient": wallet,
                    "amount_raw": 50,
                }
            }

            class ChangedSwapReceiptAdapter(FakeAdapter):
                def get_receipt(self, tx_hash):
                    if tx_hash == "0xswap":
                        return TxReceipt(
                            tx_hash=tx_hash,
                            status=1,
                            confirmations=1,
                            block_number=10,
                            block_hash="0xnew",
                            transfers=(
                                TokenTransfer(
                                    destination,
                                    "0x4444444444444444444444444444444444444444",
                                    wallet,
                                    50,
                                ),
                            ),
                        )
                    return TxReceipt(
                        tx_hash=tx_hash,
                        status=1,
                        confirmations=1,
                    )

                def allowance_raw(self):
                    return 0

            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    ChangedSwapReceiptAdapter,
                ):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(
                report["state_failure_reason"],
                "ACCEPTED_RECEIPT_BLOCK_CHANGED",
            )

    def test_doctor_measures_heartbeat_age_after_inflight_receipt_probe(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.heartbeat_path.write_text(
                json.dumps(
                    {
                        "schema": "tapeout-harvester-heartbeat/v1",
                        "updated_at": 830.0,
                        "status": "OK",
                        "result_status": "PENDING_CONFIRMATIONS",
                    }
                ),
                encoding="utf-8",
            )
            inflight = self.inflight(cfg, "CLAIM_INTENT", "claim")
            clock = [1000.0]
            wallet = "0x1111111111111111111111111111111111111111"
            reward = "0x3333333333333333333333333333333333333333"

            class SlowReceiptAdapter(FakeAdapter):
                def get_receipt(self, tx_hash):
                    clock[0] += 60.0
                    return TxReceipt(
                        tx_hash=tx_hash,
                        status=1,
                        confirmations=1,
                        transfers=(
                            TokenTransfer(
                                reward,
                                "0x0000000000000000000000000000000000000000",
                                wallet,
                                123,
                            ),
                        ),
                    )

            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    SlowReceiptAdapter,
                ):
                    with patch(
                        "tapeout_harvester.cli.time.time",
                        side_effect=lambda: clock[0],
                    ):
                        report = _doctor(cfg)
            self.assertEqual(report["heartbeat_age_seconds"], 230.0)
            self.assertEqual(report["watch_status"], "STALE")
            self.assertEqual(report["status"], "DEGRADED")

    def test_doctor_degrades_approve_when_reward_balance_was_spent(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_sell_cfg(td)
            inflight = self.inflight(cfg, "APPROVE_INTENT", "approve")
            inflight["sell_raw"] = 100
            inflight["accepted_receipts"] = {
                "claim": {
                    "tx_hash": "0xclaim",
                    "block_number": 10,
                    "block_hash": "0xblock",
                    "token": cfg.reward_token,
                    "recipient": cfg.wallet,
                    "amount_raw": 100,
                }
            }

            class SpentRewardAdapter(FakeAdapter):
                def get_receipt(self, tx_hash):
                    if tx_hash == "0xclaim":
                        return TxReceipt(
                            tx_hash=tx_hash,
                            status=1,
                            confirmations=1,
                            block_number=10,
                            block_hash="0xblock",
                            transfers=(
                                TokenTransfer(
                                    cfg.reward_token,
                                    "0x0000000000000000000000000000000000000000",
                                    cfg.wallet,
                                    100,
                                ),
                            ),
                        )
                    return TxReceipt(tx_hash=tx_hash, status=1, confirmations=1)

                def allowance_raw(self):
                    return 100

                def reward_balance_raw(self):
                    return 0

            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    SpentRewardAdapter,
                ):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(
                report["state_failure_reason"],
                "INSUFFICIENT_REWARD_BALANCE_FOR_SWAP",
            )

    def test_doctor_degrades_post_swap_cleanup_when_wrapped_balance_was_spent(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_sell_cfg(td, unwrap_native=True)
            inflight = self.inflight(
                cfg,
                "CLEANUP_REVOKE_INTENT",
                "cleanup_revoke",
            )
            inflight["cleanup_continue"] = "POST_SWAP"
            inflight["received_raw"] = 50
            inflight["accepted_receipts"] = {
                "swap": {
                    "tx_hash": "0xswap",
                    "block_number": 10,
                    "block_hash": "0xblock",
                    "token": cfg.destination_token,
                    "recipient": cfg.wallet,
                    "amount_raw": 50,
                }
            }

            class SpentWrappedAdapter(FakeAdapter):
                def get_receipt(self, tx_hash):
                    if tx_hash == "0xswap":
                        return TxReceipt(
                            tx_hash=tx_hash,
                            status=1,
                            confirmations=1,
                            block_number=10,
                            block_hash="0xblock",
                            transfers=(
                                TokenTransfer(
                                    str(cfg.destination_token),
                                    str(cfg.router),
                                    cfg.wallet,
                                    50,
                                ),
                            ),
                        )
                    return TxReceipt(tx_hash=tx_hash, status=1, confirmations=1)

                def allowance_raw(self):
                    return 0

                def destination_balance_raw(self):
                    return 0

            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch(
                    "tapeout_harvester.cli.Web3TapeOutAdapter",
                    SpentWrappedAdapter,
                ):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(
                report["state_failure_reason"],
                "INSUFFICIENT_WRAPPED_BALANCE_FOR_UNWRAP",
            )

    def test_doctor_degrades_signed_not_broadcast(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            inflight = {
                "schema": "tapeout-harvester-state/v2",
                "stage": "SIGNED_NOT_BROADCAST",
                "cycle_identity_hash": cfg.cycle_identity_hash(),
                "cycle_identity": cfg.cycle_identity(),
                "not_broadcast_reason": "CLAIM_PRE_BROADCAST_FAILED",
            }
            with patch("tapeout_harvester.cli.load_state", return_value=inflight):
                with patch("tapeout_harvester.cli.Web3TapeOutAdapter", FakeAdapter):
                    report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(
                report["state_failure_reason"],
                "CLAIM_PRE_BROADCAST_FAILED",
            )

    def test_doctor_degrades_blocked_result_even_from_old_ok_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.heartbeat_path.write_text(
                json.dumps(
                    {
                        "schema": "tapeout-harvester-heartbeat/v1",
                        "updated_at": time.time(),
                        "status": "OK",
                        "result_status": "BLOCKED",
                    }
                ),
                encoding="utf-8",
            )
            with patch("tapeout_harvester.cli.Web3TapeOutAdapter", FakeAdapter):
                report = _doctor(cfg)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(report["watch_status"], "DEGRADED")
            self.assertEqual(report["watch_result_status"], "BLOCKED")

    def test_doctor_measures_heartbeat_age_after_rpc_probe(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = self.make_cfg(td)
            cfg.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            cfg.heartbeat_path.write_text(
                json.dumps(
                    {
                        "schema": "tapeout-harvester-heartbeat/v1",
                        "updated_at": 830.0,
                        "status": "OK",
                    }
                ),
                encoding="utf-8",
            )
            clock = [1000.0]

            class SlowAdapter(FakeAdapter):
                def __init__(self, config):
                    super().__init__(config)
                    clock[0] += 60.0

            with patch("tapeout_harvester.cli.Web3TapeOutAdapter", SlowAdapter):
                with patch("tapeout_harvester.cli.time.time", side_effect=lambda: clock[0]):
                    report = _doctor(cfg)
            self.assertEqual(report["heartbeat_age_seconds"], 230.0)
            self.assertEqual(report["status"], "DEGRADED")
            self.assertEqual(report["watch_status"], "STALE")


if __name__ == "__main__":
    unittest.main()
