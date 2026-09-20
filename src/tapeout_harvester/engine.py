from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import Any, Callable

from .config import HarvesterConfig
from .models import Quote, RunResult, SignedTx, TxReceipt
from .state import atomic_write_json, default_state, load_state, process_lock


class HarvesterEngine:
    def __init__(self, config: HarvesterConfig, adapter, signer_factory: Callable[[], Any] | None = None):
        self.cfg = config
        self.adapter = adapter
        self.signer_factory = signer_factory

    def _human_reward(self, raw: int) -> Decimal:
        return Decimal(raw) / (Decimal(10) ** self.cfg.reward_decimals)

    def _quote_gate(self, quote: Quote) -> None:
        age = time.time() - quote.quoted_at
        if age > self.cfg.quote_max_age_seconds:
            raise RuntimeError("QUOTE_STALE")
        if quote.deviation_bps > self.cfg.hard_slippage_bps:
            raise RuntimeError("QUOTE_DEVIATION_EXCEEDS_HARD_MAX")
        if quote.amount_out_raw <= 0 or quote.min_out_raw <= 0:
            raise RuntimeError("QUOTE_INVALID")

    @staticmethod
    def _tx_max_cost_wei(tx: dict[str, Any]) -> int:
        return int(tx.get("gas", 0)) * int(tx.get("gasPrice", 0))

    @property
    def _gas_limit_wei(self) -> int:
        return int(self.cfg.max_gas_native_per_cycle * Decimal(10**18))

    @property
    def _native_reserve_wei(self) -> int:
        return int(self.cfg.gas_reserve_native * Decimal(10**18))

    def _check_gas_budget(
        self,
        state: dict[str, Any],
        txs: list[dict[str, Any]],
        *,
        include_cleanup_reserve: bool = True,
    ) -> int:
        future = sum(self._tx_max_cost_wei(tx) for tx in txs)
        cleanup = int(state.get("cleanup_reserve_wei") or 0) if include_cleanup_reserve else 0
        committed = int(state.get("gas_committed_wei") or 0)
        if committed + future + cleanup > self._gas_limit_wei:
            raise RuntimeError("MAX_GAS_PER_CYCLE_EXCEEDED")
        native = int(self.adapter.native_balance_wei())
        if native - future - cleanup < self._native_reserve_wei:
            raise RuntimeError("NATIVE_GAS_RESERVE_WOULD_BE_BREACHED")
        return future

    def dry_run(self) -> RunResult:
        pending = int(self.adapter.pending_raw())
        if self._human_reward(pending) < self.cfg.minimum_claim:
            return RunResult("SKIP", "BELOW_MINIMUM_CLAIM", live=False, pending_raw=pending)

        state = default_state()
        claim_tx = self.adapter.claim_tx()
        self._check_gas_budget(state, [claim_tx])
        details: dict[str, Any] = {
            "claim_max_gas_native": str(
                Decimal(self._tx_max_cost_wei(claim_tx)) / Decimal(10**18)
            ),
            "post_claim_actions_simulated": False,
        }

        if self.cfg.selling_enabled:
            sell_plan_raw = int(Decimal(pending) * self.cfg.sell_fraction)
            if sell_plan_raw <= 0:
                return RunResult("SKIP", "SELL_AMOUNT_ROUNDS_TO_ZERO", live=False, pending_raw=pending)
            quote = self.adapter.quote(sell_plan_raw)
            self._quote_gate(quote)
            details["sell_preview"] = {
                "amount_in_raw": quote.amount_in_raw,
                "amount_out_raw": quote.amount_out_raw,
                "min_out_raw": quote.min_out_raw,
                "execution_price": str(quote.execution_price),
                "spot_price": str(quote.spot_price),
                "deviation_bps": str(quote.deviation_bps),
                "note": "approval/swap simulation is deferred until claim is confirmed",
            }

        return RunResult(
            "DRY_RUN_READY",
            live=False,
            signer_loaded=False,
            pending_raw=pending,
            details=details,
        )

    def run(self, live: bool = False) -> RunResult:
        if not live:
            return self.dry_run()
        if not self.cfg.live_enabled:
            return RunResult("BLOCKED", "CONFIG_LIVE_ENABLED_FALSE", live=True)
        if self.signer_factory is None:
            return RunResult("BLOCKED", "NO_LOCAL_SIGNER_CONFIGURED", live=True)

        with process_lock(self.cfg.wallet_lock_path, require_private_parent=True):
            with process_lock(self.cfg.lock_path):
                state = load_state(self.cfg.state_path)
                return self._run_live_locked(state)

    def _save(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.cfg.state_path, state)

    def _identity_matches(self, state: dict[str, Any]) -> bool:
        return (
            state.get("cycle_identity_hash") == self.cfg.cycle_identity_hash()
            and state.get("cycle_identity") == self.cfg.cycle_identity()
        )

    def _run_live_locked(self, state: dict[str, Any]) -> RunResult:
        stage = str(state.get("stage") or "IDLE")
        if stage not in {"IDLE", "COMPLETE"}:
            if not self._identity_matches(state):
                return RunResult(
                    "BLOCKED",
                    "INFLIGHT_CONFIG_IDENTITY_MISMATCH",
                    live=True,
                    signer_loaded=False,
                    stage=stage,
                    tx_hashes=dict(state.get("tx_hashes") or {}),
                )
            if stage == "BLOCKED_SAFE":
                return RunResult(
                    "BLOCKED",
                    str(state.get("failure_reason") or "BLOCKED_SAFE"),
                    live=True,
                    signer_loaded=False,
                    stage=stage,
                    claimed_raw=state.get("claimed_raw"),
                    sell_raw=state.get("sell_raw"),
                    received_raw=state.get("received_raw"),
                    tx_hashes=dict(state.get("tx_hashes") or {}),
                )
            return self._resume_inflight(state)

        if stage == "COMPLETE":
            last_completed_at = state.get("last_completed_at")
            last_receipt = state.get("last_receipt")
            state = default_state()
            state["last_completed_at"] = last_completed_at
            state["last_receipt"] = last_receipt
            self._save(state)

        pending = int(self.adapter.pending_raw())
        if self._human_reward(pending) < self.cfg.minimum_claim:
            return RunResult("SKIP", "BELOW_MINIMUM_CLAIM", live=True, pending_raw=pending)

        claim_tx = self.adapter.claim_tx()
        new_state = default_state()
        new_state.update({
            "stage": "CLAIM_INTENT",
            "cycle_id": uuid.uuid4().hex,
            "cycle_identity_hash": self.cfg.cycle_identity_hash(),
            "cycle_identity": self.cfg.cycle_identity(),
        })
        self._check_gas_budget(new_state, [claim_tx])

        if self.cfg.selling_enabled:
            preview_amount = int(Decimal(pending) * self.cfg.sell_fraction)
            if preview_amount <= 0:
                return RunResult("SKIP", "SELL_AMOUNT_ROUNDS_TO_ZERO", live=True, pending_raw=pending)
            preview_quote = self.adapter.quote(preview_amount)
            self._quote_gate(preview_quote)

        account = self.signer_factory()
        return self._send_and_reconcile(
            new_state,
            "claim",
            claim_tx,
            account,
            signer_loaded=True,
        )

    def _send_and_reconcile(
        self,
        state: dict[str, Any],
        label: str,
        tx: dict[str, Any],
        account,
        signer_loaded: bool,
        *,
        is_cleanup: bool = False,
        pre_sign_check: Callable[[], None] | None = None,
        pre_broadcast_check: Callable[[], None] | None = None,
    ) -> RunResult:
        if pre_sign_check is not None:
            pre_sign_check()

        if is_cleanup:
            reserve = int(state.get("cleanup_reserve_wei") or 0)
            cost = self._tx_max_cost_wei(tx)
            if reserve and cost > reserve:
                return self._mark_blocked_safe(
                    state,
                    "CLEANUP_TRANSACTION_EXCEEDS_RESERVED_GAS",
                    signer_loaded=signer_loaded,
                )
            self._check_gas_budget(state, [tx], include_cleanup_reserve=False)
        else:
            self._check_gas_budget(state, [tx], include_cleanup_reserve=True)

        signed: SignedTx = self.adapter.sign(tx, account)
        cost = self._tx_max_cost_wei(tx)
        state["gas_committed_wei"] = int(state.get("gas_committed_wei") or 0) + cost
        if is_cleanup:
            state["cleanup_reserve_wei"] = 0

        state["stage"] = f"{label.upper()}_INTENT"
        state.setdefault("tx_hashes", {})[label] = signed.tx_hash
        state.setdefault("tx_intent_at", {})[label] = time.time()
        self._save(state)

        if pre_broadcast_check is not None:
            try:
                pre_broadcast_check()
            except Exception as exc:
                state.setdefault("not_broadcast_hashes", {})[label] = signed.tx_hash
                state["not_broadcast_label"] = label
                state["not_broadcast_reason"] = f"{label.upper()}_PRE_BROADCAST_FAILED"
                state["gas_committed_wei"] = max(
                    0,
                    int(state.get("gas_committed_wei") or 0) - cost,
                )
                state.get("tx_hashes", {}).pop(label, None)
                state.get("tx_intent_at", {}).pop(label, None)
                state["stage"] = "SIGNED_NOT_BROADCAST"
                self._save(state)
                raise RuntimeError("PRE_BROADCAST_CHECK_FAILED") from exc

        try:
            self.adapter.broadcast(signed)
        except Exception as exc:
            return RunResult(
                "BLOCKED_UNKNOWN",
                "BROADCAST_RESULT_UNKNOWN",
                live=True,
                signer_loaded=signer_loaded,
                stage=state["stage"],
                tx_hashes=dict(state["tx_hashes"]),
                details={"error": type(exc).__name__},
            )
        return self._reconcile_stage(state, account=account, signer_loaded=signer_loaded)

    def _resume_inflight(self, state: dict[str, Any]) -> RunResult:
        if str(state.get("stage") or "") == "SIGNED_NOT_BROADCAST":
            label = str(state.get("not_broadcast_label") or "unknown")
            reason = str(
                state.get("not_broadcast_reason")
                or f"{label.upper()}_PRE_BROADCAST_FAILED"
            )
            if bool(state.get("approval_created_by_cycle")):
                return self._begin_cleanup(
                    state,
                    reason,
                    account=None,
                    signer_loaded=False,
                    continuation="BLOCK",
                )
            return self._mark_blocked_safe(
                state,
                reason,
                signer_loaded=False,
            )
        return self._reconcile_stage(state, account=None, signer_loaded=False)

    @staticmethod
    def _current_label(stage: str) -> str:
        return stage.removesuffix("_INTENT").lower()

    def _reconcile_stage(
        self,
        state: dict[str, Any],
        account=None,
        signer_loaded: bool = False,
    ) -> RunResult:
        stage = str(state.get("stage") or "")
        label = self._current_label(stage)
        tx_hash = (state.get("tx_hashes") or {}).get(label)
        if not tx_hash:
            return RunResult("BLOCKED", "INFLIGHT_STATE_MISSING_TX_HASH", live=True, stage=stage)

        try:
            receipt: TxReceipt | None = self.adapter.get_receipt(tx_hash)
        except Exception as exc:
            return RunResult(
                "BLOCKED_UNKNOWN",
                "RECEIPT_QUERY_FAILED_NO_REBROADCAST",
                live=True,
                signer_loaded=False,
                stage=stage,
                tx_hashes=dict(state.get("tx_hashes") or {}),
                details={"error": type(exc).__name__},
            )
        if receipt is None:
            intent_at = (state.get("tx_intent_at") or {}).get(label)
            if intent_at is None:
                return RunResult(
                    "BLOCKED_UNKNOWN",
                    "MISSING_TX_INTENT_TIMESTAMP_NO_REBROADCAST",
                    live=True,
                    signer_loaded=False,
                    stage=stage,
                    tx_hashes=dict(state.get("tx_hashes") or {}),
                )
            age = max(0.0, time.time() - float(intent_at))
            if age > self.cfg.tx_receipt_timeout_seconds:
                return RunResult(
                    "BLOCKED_UNKNOWN",
                    "RECEIPT_TIMEOUT_NO_REBROADCAST",
                    live=True,
                    signer_loaded=False,
                    stage=stage,
                    tx_hashes=dict(state.get("tx_hashes") or {}),
                    details={"tx_age_seconds": round(age, 3)},
                )
            return RunResult(
                "PENDING_TX",
                "NO_RECEIPT_YET_NO_REBROADCAST",
                live=True,
                signer_loaded=False,
                stage=stage,
                tx_hashes=dict(state.get("tx_hashes") or {}),
                details={"tx_age_seconds": round(age, 3)},
            )

        if receipt.confirmations < self.cfg.min_confirmations:
            return RunResult(
                "PENDING_CONFIRMATIONS",
                "RECEIPT_NOT_FINAL_ENOUGH",
                live=True,
                signer_loaded=False,
                stage=stage,
                tx_hashes=dict(state.get("tx_hashes") or {}),
                details={
                    "confirmations": receipt.confirmations,
                    "required_confirmations": self.cfg.min_confirmations,
                    "block_number": receipt.block_number,
                },
            )

        if receipt.status != 1:
            if label == "swap" and bool(state.get("approval_created_by_cycle")):
                return self._begin_cleanup(
                    state,
                    "SWAP_TX_REVERTED",
                    account,
                    signer_loaded,
                    continuation="BLOCK",
                )
            if label == "cleanup_revoke":
                return self._mark_blocked_safe(
                    state,
                    "CLEANUP_REVOKE_TX_REVERTED",
                    signer_loaded=signer_loaded,
                )
            return self._mark_blocked_safe(
                state,
                f"{label.upper()}_TX_REVERTED",
                signer_loaded=signer_loaded,
            )

        if label == "claim":
            return self._after_claim(state, receipt, account, signer_loaded)
        if label == "approve_reset":
            return self._after_approve_reset(state, account, signer_loaded)
        if label == "approve":
            return self._after_approve(state, account, signer_loaded)
        if label == "swap":
            return self._after_swap(state, receipt, account, signer_loaded)
        if label == "cleanup_revoke":
            return self._after_cleanup_revoke(state, account, signer_loaded)
        if label == "unwrap":
            return self._after_unwrap(state, receipt, signer_loaded)
        return self._mark_blocked_safe(state, "UNKNOWN_STATE_STAGE", signer_loaded=signer_loaded)

    def _ensure_signer(self, account, signer_loaded: bool):
        if account is not None:
            return account, signer_loaded
        if self.signer_factory is None:
            raise RuntimeError("NO_LOCAL_SIGNER_CONFIGURED")
        return self.signer_factory(), True

    def _mark_blocked_safe(
        self,
        state: dict[str, Any],
        reason: str,
        *,
        signer_loaded: bool,
        cleaned: bool = False,
    ) -> RunResult:
        state["stage"] = "BLOCKED_SAFE"
        state["failure_reason"] = reason
        if int(state.get("cleanup_reserve_wei") or 0) and not bool(state.get("approval_created_by_cycle")):
            state["cleanup_reserve_wei"] = 0
        self._save(state)
        return RunResult(
            "BLOCKED_CLEANED" if cleaned else "BLOCKED",
            reason,
            live=True,
            signer_loaded=signer_loaded,
            stage="BLOCKED_SAFE",
            claimed_raw=state.get("claimed_raw"),
            sell_raw=state.get("sell_raw"),
            received_raw=state.get("received_raw"),
            tx_hashes=dict(state.get("tx_hashes") or {}),
        )

    def _prepare_cleanup_reserve(self, state: dict[str, Any]) -> None:
        preview = self.adapter.approve_tx(0)
        gas_limit = max(int(preview.get("gas", 0)) * 2, int(preview.get("gas", 0)) + 30_000)
        observed_gas_price = int(preview.get("gasPrice", 0))
        gas_price = observed_gas_price * 2
        reserve = gas_limit * gas_price
        existing = int(state.get("cleanup_reserve_wei") or 0)
        if existing and reserve > existing:
            return
        state["cleanup_gas_limit"] = gas_limit
        state["cleanup_gas_price_wei"] = gas_price
        state["cleanup_reserve_wei"] = reserve
        self._check_gas_budget(state, [], include_cleanup_reserve=True)
        self._save(state)

    def _build_reserved_cleanup_tx(self, state: dict[str, Any]) -> dict[str, Any]:
        gas_limit = state.get("cleanup_gas_limit")
        gas_price = state.get("cleanup_gas_price_wei")
        if not gas_limit or not gas_price:
            raise RuntimeError("CLEANUP_GAS_RESERVATION_MISSING")
        return self.adapter.approve_tx(
            0,
            gas_limit=int(gas_limit),
            gas_price_wei=int(gas_price),
        )

    def _begin_cleanup(
        self,
        state: dict[str, Any],
        reason: str,
        account,
        signer_loaded: bool,
        *,
        continuation: str,
    ) -> RunResult:
        try:
            allowance = int(self.adapter.allowance_raw())
        except Exception:
            return self._mark_blocked_safe(
                state,
                "CLEANUP_ALLOWANCE_READ_FAILED",
                signer_loaded=signer_loaded,
            )

        state["cleanup_reason"] = reason
        state["cleanup_continue"] = continuation
        if allowance == 0:
            state["approval_created_by_cycle"] = False
            state["cleanup_reserve_wei"] = 0
            self._save(state)
            return self._continue_after_cleanup(state, account, signer_loaded)

        account, signer_loaded = self._ensure_signer(account, signer_loaded)
        try:
            tx = self._build_reserved_cleanup_tx(state)
        except Exception:
            return self._mark_blocked_safe(
                state,
                "CLEANUP_BUILD_FAILED",
                signer_loaded=signer_loaded,
            )
        return self._send_and_reconcile(
            state,
            "cleanup_revoke",
            tx,
            account,
            signer_loaded,
            is_cleanup=True,
        )

    def _after_cleanup_revoke(
        self,
        state: dict[str, Any],
        account,
        signer_loaded: bool,
    ) -> RunResult:
        if int(self.adapter.allowance_raw()) != 0:
            return self._mark_blocked_safe(
                state,
                "CLEANUP_ALLOWANCE_NOT_ZERO",
                signer_loaded=signer_loaded,
            )
        state["approval_created_by_cycle"] = False
        state["cleanup_reserve_wei"] = 0
        self._save(state)
        return self._continue_after_cleanup(state, account, signer_loaded)

    def _continue_after_cleanup(
        self,
        state: dict[str, Any],
        account,
        signer_loaded: bool,
    ) -> RunResult:
        continuation = str(state.get("cleanup_continue") or "BLOCK")
        reason = str(state.get("cleanup_reason") or "SELL_PATH_ABORTED")
        if continuation == "POST_SWAP":
            if self.cfg.unwrap_native:
                return self._send_unwrap(state, account, signer_loaded)
            return self._finish(state, signer_loaded=signer_loaded)
        return self._mark_blocked_safe(
            state,
            reason,
            signer_loaded=signer_loaded,
            cleaned=True,
        )

    def _record_transfer_receipt(
        self,
        state: dict[str, Any],
        label: str,
        receipt: TxReceipt,
        *,
        token: str,
        recipient: str,
        amount_raw: int,
    ) -> None:
        state.setdefault("accepted_receipts", {})[label] = {
            "tx_hash": receipt.tx_hash,
            "block_number": receipt.block_number,
            "block_hash": receipt.block_hash,
            "token": token.lower(),
            "recipient": recipient.lower(),
            "amount_raw": int(amount_raw),
        }
        self._save(state)

    def _revalidate_transfer_receipt(
        self,
        state: dict[str, Any],
        label: str,
    ) -> None:
        fact = (state.get("accepted_receipts") or {}).get(label)
        if not fact:
            raise RuntimeError("ACCEPTED_RECEIPT_FACT_MISSING")
        receipt = self.adapter.get_receipt(str(fact["tx_hash"]))
        if receipt is None:
            raise RuntimeError("ACCEPTED_RECEIPT_NOT_CANONICAL")
        if receipt.status != 1 or receipt.confirmations < self.cfg.min_confirmations:
            raise RuntimeError("ACCEPTED_RECEIPT_NOT_FINAL_ENOUGH")
        if receipt.block_hash != fact.get("block_hash"):
            raise RuntimeError("ACCEPTED_RECEIPT_BLOCK_CHANGED")
        amount = receipt.net_received_raw(str(fact["token"]), str(fact["recipient"]))
        if amount != int(fact["amount_raw"]):
            raise RuntimeError("ACCEPTED_RECEIPT_AMOUNT_CHANGED")

    def _swap_prerequisite_gate(self, state: dict[str, Any], quote: Quote) -> None:
        self._revalidate_transfer_receipt(state, "claim")
        self._quote_gate(quote)

    def _unwrap_prerequisite_gate(self, state: dict[str, Any]) -> None:
        self._revalidate_transfer_receipt(state, "swap")

    def _after_claim(
        self,
        state: dict[str, Any],
        receipt: TxReceipt,
        account,
        signer_loaded: bool,
    ) -> RunResult:
        claimed = receipt.net_received_raw(self.cfg.reward_token, self.cfg.wallet)
        if claimed <= 0:
            return self._mark_blocked_safe(
                state,
                "CLAIM_RECEIPT_HAS_NO_ATTRIBUTED_REWARD_TRANSFER",
                signer_loaded=signer_loaded,
            )
        state["claimed_raw"] = claimed
        self._record_transfer_receipt(
            state,
            "claim",
            receipt,
            token=self.cfg.reward_token,
            recipient=self.cfg.wallet,
            amount_raw=claimed,
        )

        if not self.cfg.selling_enabled:
            state["sell_raw"] = 0
            self._save(state)
            return self._finish(state, signer_loaded=signer_loaded)

        sell_raw = int(Decimal(claimed) * self.cfg.sell_fraction)
        if sell_raw <= 0 or int(self.adapter.reward_balance_raw()) < sell_raw:
            return self._mark_blocked_safe(
                state,
                "INVALID_POST_CLAIM_SELL_AMOUNT",
                signer_loaded=signer_loaded,
            )
        state["sell_raw"] = sell_raw
        self._save(state)
        return self._prepare_sell(state, account, signer_loaded)

    def _prepare_sell(
        self,
        state: dict[str, Any],
        account,
        signer_loaded: bool,
    ) -> RunResult:
        sell_raw = int(state["sell_raw"])
        try:
            quote = self.adapter.quote(sell_raw)
            self._quote_gate(quote)
            allowance = int(self.adapter.allowance_raw())
        except Exception:
            return self._mark_blocked_safe(
                state,
                "POST_CLAIM_SELL_PREFLIGHT_FAILED",
                signer_loaded=signer_loaded,
            )

        account, signer_loaded = self._ensure_signer(account, signer_loaded)
        if allowance not in (0, sell_raw):
            try:
                tx = self.adapter.approve_tx(0)
                self._check_gas_budget(state, [tx])
            except Exception:
                return self._mark_blocked_safe(
                    state,
                    "ALLOWANCE_RESET_PREP_FAILED",
                    signer_loaded=signer_loaded,
                )
            return self._send_and_reconcile(
                state,
                "approve_reset",
                tx,
                account,
                signer_loaded,
            )
        if allowance != sell_raw:
            return self._send_exact_approve(state, account, signer_loaded)
        return self._send_swap(state, quote, account, signer_loaded)

    def _send_exact_approve(
        self,
        state: dict[str, Any],
        account,
        signer_loaded: bool,
    ) -> RunResult:
        try:
            tx = self.adapter.approve_tx(int(state["sell_raw"]))
            self._prepare_cleanup_reserve(state)
            self._check_gas_budget(state, [tx], include_cleanup_reserve=True)
        except Exception:
            return self._mark_blocked_safe(
                state,
                "EXACT_APPROVAL_PREP_FAILED",
                signer_loaded=signer_loaded,
            )
        return self._send_and_reconcile(
            state,
            "approve",
            tx,
            account,
            signer_loaded,
        )

    def _after_approve_reset(
        self,
        state: dict[str, Any],
        account,
        signer_loaded: bool,
    ) -> RunResult:
        if int(self.adapter.allowance_raw()) != 0:
            return self._mark_blocked_safe(
                state,
                "ALLOWANCE_RESET_FAILED",
                signer_loaded=signer_loaded,
            )
        account, signer_loaded = self._ensure_signer(account, signer_loaded)
        return self._send_exact_approve(state, account, signer_loaded)

    def _after_approve(
        self,
        state: dict[str, Any],
        account,
        signer_loaded: bool,
    ) -> RunResult:
        sell_raw = int(state["sell_raw"])
        state["approval_created_by_cycle"] = True
        self._save(state)
        try:
            allowance = int(self.adapter.allowance_raw())
        except Exception:
            return RunResult(
                "BLOCKED_UNKNOWN",
                "ALLOWANCE_QUERY_FAILED_AFTER_APPROVAL",
                live=True,
                signer_loaded=False,
                stage=state.get("stage"),
                tx_hashes=dict(state.get("tx_hashes") or {}),
            )
        if allowance != sell_raw:
            if allowance != 0:
                return self._begin_cleanup(
                    state,
                    "EXACT_ALLOWANCE_MISMATCH",
                    account,
                    signer_loaded,
                    continuation="BLOCK",
                )
            state["approval_created_by_cycle"] = False
            state["cleanup_reserve_wei"] = 0
            self._save(state)
            return self._mark_blocked_safe(
                state,
                "EXACT_ALLOWANCE_MISMATCH",
                signer_loaded=signer_loaded,
            )

        try:
            self._revalidate_transfer_receipt(state, "claim")
            self._prepare_cleanup_reserve(state)
            quote = self.adapter.quote(sell_raw)
            self._quote_gate(quote)
            account, signer_loaded = self._ensure_signer(account, signer_loaded)
            return self._send_swap(state, quote, account, signer_loaded)
        except Exception:
            return self._begin_cleanup(
                state,
                "POST_APPROVAL_SELL_PREP_FAILED",
                account,
                signer_loaded,
                continuation="BLOCK",
            )

    def _send_swap(
        self,
        state: dict[str, Any],
        quote: Quote,
        account,
        signer_loaded: bool,
    ) -> RunResult:
        try:
            self._quote_gate(quote)
            tx = self.adapter.swap_tx(quote)
            self._quote_gate(quote)
            self._check_gas_budget(state, [tx], include_cleanup_reserve=True)
            return self._send_and_reconcile(
                state,
                "swap",
                tx,
                account,
                signer_loaded,
                pre_sign_check=lambda: self._swap_prerequisite_gate(state, quote),
                pre_broadcast_check=lambda: self._swap_prerequisite_gate(state, quote),
            )
        except Exception:
            if bool(state.get("approval_created_by_cycle")):
                return self._begin_cleanup(
                    state,
                    "SWAP_PRE_BROADCAST_FAILED",
                    account,
                    signer_loaded,
                    continuation="BLOCK",
                )
            return self._mark_blocked_safe(
                state,
                "SWAP_PRE_BROADCAST_FAILED",
                signer_loaded=signer_loaded,
            )

    def _after_swap(
        self,
        state: dict[str, Any],
        receipt: TxReceipt,
        account,
        signer_loaded: bool,
    ) -> RunResult:
        received = receipt.net_received_raw(
            str(self.cfg.destination_token),
            self.cfg.wallet,
        )
        if received <= 0:
            return self._begin_cleanup(
                state,
                "SWAP_RECEIPT_HAS_NO_ATTRIBUTED_DESTINATION_TRANSFER",
                account,
                signer_loaded,
                continuation="BLOCK",
            )
        state["received_raw"] = received
        self._record_transfer_receipt(
            state,
            "swap",
            receipt,
            token=str(self.cfg.destination_token),
            recipient=self.cfg.wallet,
            amount_raw=received,
        )

        allowance = int(self.adapter.allowance_raw())
        if allowance != 0:
            return self._begin_cleanup(
                state,
                "POST_SWAP_RESIDUAL_ALLOWANCE",
                account,
                signer_loaded,
                continuation="POST_SWAP",
            )

        state["approval_created_by_cycle"] = False
        state["cleanup_reserve_wei"] = 0
        self._save(state)
        if self.cfg.unwrap_native:
            return self._send_unwrap(state, account, signer_loaded)
        return self._finish(state, signer_loaded=signer_loaded)

    def _send_unwrap(
        self,
        state: dict[str, Any],
        account,
        signer_loaded: bool,
    ) -> RunResult:
        account, signer_loaded = self._ensure_signer(account, signer_loaded)
        try:
            self._unwrap_prerequisite_gate(state)
            tx = self.adapter.unwrap_tx(int(state["received_raw"]))
            self._check_gas_budget(state, [tx], include_cleanup_reserve=False)
            return self._send_and_reconcile(
                state,
                "unwrap",
                tx,
                account,
                signer_loaded,
                pre_sign_check=lambda: self._unwrap_prerequisite_gate(state),
                pre_broadcast_check=lambda: self._unwrap_prerequisite_gate(state),
            )
        except Exception:
            return self._mark_blocked_safe(
                state,
                "UNWRAP_PREP_FAILED",
                signer_loaded=signer_loaded,
            )

    def _after_unwrap(
        self,
        state: dict[str, Any],
        receipt: TxReceipt,
        signer_loaded: bool,
    ) -> RunResult:
        amount = int(state.get("received_raw") or 0)
        withdrawn = receipt.withdrawn_raw(
            str(self.cfg.wrapped_native_token),
            self.cfg.wallet,
        )
        if amount <= 0 or withdrawn != amount:
            return self._mark_blocked_safe(
                state,
                "UNWRAP_RECEIPT_EVENT_MISMATCH",
                signer_loaded=signer_loaded,
            )
        return self._finish(state, signer_loaded=signer_loaded)

    def _finish(self, state: dict[str, Any], signer_loaded: bool) -> RunResult:
        receipt = {
            "schema": "tapeout-harvester-receipt/v2",
            "completed_at": time.time(),
            "cycle_id": state.get("cycle_id"),
            "cycle_identity_hash": state.get("cycle_identity_hash"),
            "claimed_raw": state.get("claimed_raw"),
            "sell_raw": state.get("sell_raw"),
            "received_raw": state.get("received_raw"),
            "gas_committed_wei": state.get("gas_committed_wei"),
            "tx_hashes": dict(state.get("tx_hashes") or {}),
            "live": True,
        }
        self.cfg.receipt_dir.mkdir(parents=True, exist_ok=True)
        path = self.cfg.receipt_dir / (
            f"receipt-{int(time.time())}-{str(state.get('cycle_id'))[:8]}.json"
        )
        atomic_write_json(path, receipt)

        state["stage"] = "COMPLETE"
        state["last_completed_at"] = receipt["completed_at"]
        state["last_receipt"] = str(path)
        state["cleanup_reserve_wei"] = 0
        state["approval_created_by_cycle"] = False
        self._save(state)
        return RunResult(
            "COMPLETE",
            live=True,
            signer_loaded=signer_loaded,
            claimed_raw=state.get("claimed_raw"),
            sell_raw=state.get("sell_raw"),
            received_raw=state.get("received_raw"),
            stage="COMPLETE",
            tx_hashes=dict(state.get("tx_hashes") or {}),
            details={
                "receipt": path.name,
                "gas_committed_wei": state.get("gas_committed_wei"),
            },
        )
