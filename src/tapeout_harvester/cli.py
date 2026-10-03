from __future__ import annotations

import argparse
import json
import os
import re
import time

from .adapter import Web3TapeOutAdapter
from .config import HarvesterConfig
from .engine import HarvesterEngine
from .signer import load_local_account, store_keystore_password
from .state import atomic_write_json, load_state, process_lock


_INTERNAL_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*(?::[A-Za-z0-9_.-]+)?$")
_HEALTHY_RUN_STATUSES = frozenset(
    {"SKIP", "DRY_RUN_READY", "COMPLETE", "PENDING_TX", "PENDING_CONFIRMATIONS"}
)


def _sanitize_error_message(exc: Exception) -> str:
    text = str(exc).strip()
    if isinstance(exc, ValueError):
        return "CONFIG_INVALID"
    if len(text) <= 96 and _INTERNAL_ERROR_CODE_RE.fullmatch(text):
        return text
    return "RUNTIME_DETAIL_REDACTED"


def _engine(cfg: HarvesterConfig) -> HarvesterEngine:
    adapter = Web3TapeOutAdapter(cfg)

    def signer_factory():
        if not (cfg.keystore_path and cfg.keyring_service and cfg.keyring_account):
            raise RuntimeError("SIGNER_CONFIG_REQUIRED_FOR_LIVE_MODE")
        return load_local_account(cfg.keystore_path, cfg.keyring_service, cfg.keyring_account)

    return HarvesterEngine(cfg, adapter, signer_factory=signer_factory)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


def _heartbeat_status_for_result(result_status: str) -> str:
    return "OK" if result_status in _HEALTHY_RUN_STATUSES else "DEGRADED"


def _write_heartbeat(
    cfg: HarvesterConfig,
    *,
    status: str,
    live: bool,
    adapter: Web3TapeOutAdapter | None = None,
    result_status: str | None = None,
    error: Exception | None = None,
) -> None:
    payload = {
        "schema": "tapeout-harvester-heartbeat/v1",
        "updated_at": time.time(),
        "pid": os.getpid(),
        "status": status,
        "live": bool(live),
        "result_status": result_status,
        "cycle_identity_hash": cfg.cycle_identity_hash(),
        "watcher_identity_hash": cfg.watcher_identity_hash(),
        "rpc": adapter.rpc_label if adapter is not None else None,
        "error": type(error).__name__ if error is not None else None,
        "message": _sanitize_error_message(error) if error is not None else None,
    }
    with process_lock(cfg.heartbeat_lock_path, blocking=True):
        atomic_write_json(cfg.heartbeat_path, payload)


def _accepted_transfer_receipt_issue(
    cfg: HarvesterConfig,
    state: dict,
    label: str,
    adapter: Web3TapeOutAdapter,
) -> str | None:
    fact = (state.get("accepted_receipts") or {}).get(label)
    if not fact:
        return "ACCEPTED_RECEIPT_FACT_MISSING"
    try:
        receipt = adapter.get_receipt(str(fact["tx_hash"]))
    except Exception:
        return "ACCEPTED_RECEIPT_QUERY_FAILED"
    if receipt is None:
        return "ACCEPTED_RECEIPT_NOT_CANONICAL"
    if receipt.status != 1 or receipt.confirmations < cfg.min_confirmations:
        return "ACCEPTED_RECEIPT_NOT_FINAL_ENOUGH"
    if receipt.block_hash != fact.get("block_hash"):
        return "ACCEPTED_RECEIPT_BLOCK_CHANGED"
    amount = receipt.net_received_raw(str(fact["token"]), str(fact["recipient"]))
    if amount != int(fact["amount_raw"]):
        return "ACCEPTED_RECEIPT_AMOUNT_CHANGED"
    return None


def _confirmed_inflight_issue(
    cfg: HarvesterConfig,
    state: dict,
    label: str,
    receipt,
    adapter: Web3TapeOutAdapter,
) -> str | None:
    """Mirror deterministic, read-only post-receipt recovery invariants."""
    if label == "claim":
        claimed = int(receipt.net_received_raw(cfg.reward_token, cfg.wallet))
        if claimed <= 0:
            return "CLAIM_RECEIPT_HAS_NO_ATTRIBUTED_REWARD_TRANSFER"
        if cfg.selling_enabled:
            sell_raw = int(cfg.sell_fraction * claimed)
            if sell_raw <= 0:
                return "INVALID_POST_CLAIM_SELL_AMOUNT"
            try:
                reward_balance = int(adapter.reward_balance_raw())
            except Exception:
                return "POST_CLAIM_REWARD_BALANCE_QUERY_FAILED"
            if reward_balance < sell_raw:
                return "INVALID_POST_CLAIM_SELL_AMOUNT"
        return None

    if label == "approve_reset":
        try:
            allowance = int(adapter.allowance_raw())
        except Exception:
            return "ALLOWANCE_QUERY_FAILED_AFTER_RESET"
        return "ALLOWANCE_RESET_FAILED" if allowance != 0 else None

    if label == "approve":
        sell_raw = int(state.get("sell_raw") or 0)
        try:
            allowance = int(adapter.allowance_raw())
        except Exception:
            return "ALLOWANCE_QUERY_FAILED_AFTER_APPROVAL"
        if sell_raw <= 0 or allowance != sell_raw:
            return "EXACT_ALLOWANCE_MISMATCH"
        prerequisite_issue = _accepted_transfer_receipt_issue(
            cfg,
            state,
            "claim",
            adapter,
        )
        if prerequisite_issue is not None:
            return prerequisite_issue
        try:
            reward_balance = int(adapter.reward_balance_raw())
        except Exception:
            return "POST_APPROVAL_REWARD_BALANCE_QUERY_FAILED"
        if reward_balance < sell_raw:
            return "INSUFFICIENT_REWARD_BALANCE_FOR_SWAP"
        return None

    if label == "swap":
        if cfg.destination_token is None:
            return "SWAP_RECEIPT_HAS_NO_ATTRIBUTED_DESTINATION_TRANSFER"
        received = int(
            receipt.net_received_raw(str(cfg.destination_token), cfg.wallet)
        )
        if received <= 0:
            return "SWAP_RECEIPT_HAS_NO_ATTRIBUTED_DESTINATION_TRANSFER"
        try:
            allowance = int(adapter.allowance_raw())
        except Exception:
            return "POST_SWAP_ALLOWANCE_QUERY_FAILED"
        if allowance != 0:
            return "POST_SWAP_RESIDUAL_ALLOWANCE"
        if cfg.unwrap_native:
            try:
                destination_balance = int(adapter.destination_balance_raw())
            except Exception:
                return "POST_SWAP_DESTINATION_BALANCE_QUERY_FAILED"
            if destination_balance < received:
                return "INSUFFICIENT_WRAPPED_BALANCE_FOR_UNWRAP"
        return None

    if label == "cleanup_revoke":
        try:
            allowance = int(adapter.allowance_raw())
        except Exception:
            return "CLEANUP_ALLOWANCE_READ_FAILED"
        if allowance != 0:
            return "CLEANUP_ALLOWANCE_NOT_ZERO"
        if str(state.get("cleanup_continue") or "BLOCK") != "POST_SWAP":
            return str(state.get("cleanup_reason") or "SELL_PATH_ABORTED")
        if cfg.unwrap_native:
            prerequisite_issue = _accepted_transfer_receipt_issue(
                cfg,
                state,
                "swap",
                adapter,
            )
            if prerequisite_issue is not None:
                return prerequisite_issue
            amount = int(state.get("received_raw") or 0)
            try:
                destination_balance = int(adapter.destination_balance_raw())
            except Exception:
                return "POST_SWAP_DESTINATION_BALANCE_QUERY_FAILED"
            if amount <= 0 or destination_balance < amount:
                return "INSUFFICIENT_WRAPPED_BALANCE_FOR_UNWRAP"
        return None

    if label == "unwrap":
        amount = int(state.get("received_raw") or 0)
        if cfg.wrapped_native_token is None:
            return "UNWRAP_RECEIPT_EVENT_MISMATCH"
        withdrawn = int(
            receipt.withdrawn_raw(str(cfg.wrapped_native_token), cfg.wallet)
        )
        return (
            "UNWRAP_RECEIPT_EVENT_MISMATCH"
            if amount <= 0 or withdrawn != amount
            else None
        )

    return "UNKNOWN_STATE_STAGE"


def _doctor_snapshot(
    cfg: HarvesterConfig,
    state: dict,
    adapter: Web3TapeOutAdapter,
    pending_raw: int,
) -> dict:
    state_stage = str(state.get("stage") or "IDLE")
    state_failure_reason = state.get("failure_reason")
    state_health = "DEGRADED" if state_stage == "BLOCKED_SAFE" else "OK"
    if state_stage not in {"IDLE", "COMPLETE", "BLOCKED_SAFE"}:
        identity_matches = (
            state.get("cycle_identity_hash") == cfg.cycle_identity_hash()
            and state.get("cycle_identity") == cfg.cycle_identity()
        )
        if not identity_matches:
            state_health = "DEGRADED"
            state_failure_reason = "INFLIGHT_CONFIG_IDENTITY_MISMATCH"
        elif state_stage == "SIGNED_NOT_BROADCAST":
            state_health = "DEGRADED"
            state_failure_reason = str(
                state.get("not_broadcast_reason") or "SIGNED_NOT_BROADCAST"
            )
        else:
            label = state_stage.removesuffix("_INTENT").lower()
            tx_hash = (state.get("tx_hashes") or {}).get(label)
            if not tx_hash:
                state_health = "DEGRADED"
                state_failure_reason = "INFLIGHT_STATE_MISSING_TX_HASH"
            else:
                try:
                    receipt = adapter.get_receipt(str(tx_hash))
                except Exception:
                    state_health = "DEGRADED"
                    state_failure_reason = "RECEIPT_QUERY_FAILED_NO_REBROADCAST"
                else:
                    if receipt is None:
                        intent_at = (state.get("tx_intent_at") or {}).get(label)
                        if intent_at is None:
                            state_health = "DEGRADED"
                            state_failure_reason = "MISSING_TX_INTENT_TIMESTAMP_NO_REBROADCAST"
                        else:
                            age = max(0.0, time.time() - float(intent_at))
                            if age > cfg.tx_receipt_timeout_seconds:
                                state_health = "DEGRADED"
                                state_failure_reason = "RECEIPT_TIMEOUT_NO_REBROADCAST"
                    elif int(receipt.confirmations) >= cfg.min_confirmations:
                        if int(receipt.status) != 1:
                            state_health = "DEGRADED"
                            state_failure_reason = f"{label.upper()}_TX_REVERTED"
                        else:
                            state_failure_reason = _confirmed_inflight_issue(
                                cfg,
                                state,
                                label,
                                receipt,
                                adapter,
                            )
                            if state_failure_reason is not None:
                                state_health = "DEGRADED"

    heartbeat_age = None
    watch_status = "NOT_OBSERVED"
    watch_result_status = None
    if cfg.heartbeat_path.exists():
        heartbeat = json.loads(cfg.heartbeat_path.read_text(encoding="utf-8"))
        updated_at = float(heartbeat.get("updated_at") or 0)
        heartbeat_age = max(0.0, time.time() - updated_at) if updated_at > 0 else None
        stale_after = max(120, cfg.check_interval_seconds * 3)
        watch_result_status = heartbeat.get("result_status")
        heartbeat_watcher_identity_hash = heartbeat.get(
            "watcher_identity_hash"
        )
        watch_status = (
            "STALE"
            if heartbeat_age is None or heartbeat_age > stale_after
            else str(heartbeat.get("status") or "UNKNOWN")
        )
        if (
            watch_status != "STALE"
            and heartbeat_watcher_identity_hash != cfg.watcher_identity_hash()
        ):
            watch_status = "DEGRADED"
        elif (
            watch_status == "OK"
            and watch_result_status is not None
            and _heartbeat_status_for_result(str(watch_result_status)) != "OK"
        ):
            watch_status = "DEGRADED"

    status = (
        "OK"
        if watch_status in {"OK", "NOT_OBSERVED"} and state_health == "OK"
        else "DEGRADED"
    )
    return {
        "status": status,
        "rpc": adapter.rpc_label,
        "rpc_count": len(cfg.rpc_urls),
        "pending_raw": pending_raw,
        "state_stage": state_stage,
        "state_health": state_health,
        "state_failure_reason": state_failure_reason,
        "inflight": state_stage not in {"IDLE", "COMPLETE"},
        "last_completed_at": state.get("last_completed_at"),
        "heartbeat_path": str(cfg.heartbeat_path),
        "heartbeat_age_seconds": heartbeat_age,
        "watch_status": watch_status,
        "watch_result_status": watch_result_status,
    }


def _doctor(cfg: HarvesterConfig) -> dict:
    adapter = Web3TapeOutAdapter(cfg)
    pending_raw = int(adapter.pending_raw())
    latest_state = None
    for _ in range(3):
        state = load_state(cfg.state_path)
        report = _doctor_snapshot(cfg, state, adapter, pending_raw)
        latest_state = load_state(cfg.state_path)
        if latest_state == state:
            return report

    state_stage = str((latest_state or {}).get("stage") or "IDLE")
    report.update(
        {
            "status": "DEGRADED",
            "state_stage": state_stage,
            "state_health": "DEGRADED",
            "state_failure_reason": "STATE_CHANGED_DURING_DIAGNOSIS",
            "inflight": state_stage not in {"IDLE", "COMPLETE"},
            "last_completed_at": (latest_state or {}).get("last_completed_at"),
        }
    )
    return report


def main() -> int:
    p = argparse.ArgumentParser(prog="tapeout-harvester")
    p.add_argument("--config", default="config.toml")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate config and print a sanitized summary")
    sub.add_parser("doctor", help="read-only RPC/state/watch health check")
    run = sub.add_parser("run", help="run one cycle; dry-run unless --live")
    run.add_argument("--live", action="store_true")
    watch = sub.add_parser("watch", help="repeat cycles at configured interval")
    watch.add_argument("--live", action="store_true")
    sub.add_parser("state", help="show local idempotency state")
    signer = sub.add_parser("signer", help="local signer setup")
    signer.add_argument("action", choices=["store-password"])
    args = p.parse_args()

    try:
        cfg = HarvesterConfig.load(args.config)
        if args.command == "check":
            _print({
                "status": "CONFIG_OK",
                "chain_id": cfg.chain_id,
                "rpc_count": len(cfg.rpc_urls),
                "miner_count": len(cfg.miner_keys),
                "minimum_claim": str(cfg.minimum_claim),
                "sell_fraction": str(cfg.sell_fraction),
                "selling_enabled": cfg.selling_enabled,
                "live_enabled": cfg.live_enabled,
                "min_confirmations": cfg.min_confirmations,
                "state_path_configured": True,
                "receipt_dir_configured": True,
            })
            return 0
        if args.command == "state":
            _print(load_state(cfg.state_path))
            return 0
        if args.command == "doctor":
            report = _doctor(cfg)
            _print(report)
            return 0 if report["status"] == "OK" else 2
        if args.command == "signer":
            if not (cfg.keyring_service and cfg.keyring_account):
                raise RuntimeError("KEYRING_SERVICE_AND_ACCOUNT_REQUIRED")
            store_keystore_password(cfg.keyring_service, cfg.keyring_account)
            _print({"status": "PASSWORD_STORED_IN_SECURE_KEYRING"})
            return 0
        if args.command == "run":
            engine = _engine(cfg)
            result = engine.run(live=bool(args.live))
            _print(result.to_dict())
            return 0 if result.status in {
                "SKIP", "DRY_RUN_READY", "COMPLETE", "PENDING_TX", "PENDING_CONFIRMATIONS"
            } else 2
        if args.command == "watch":
            while True:
                engine = None
                try:
                    # Rebuild the adapter each cycle so a provider that degraded
                    # after startup does not remain pinned forever.
                    engine = _engine(cfg)
                    result = engine.run(live=bool(args.live))
                    _write_heartbeat(
                        cfg,
                        status=_heartbeat_status_for_result(result.status),
                        live=bool(args.live),
                        adapter=engine.adapter,
                        result_status=result.status,
                    )
                    _print(result.to_dict())
                except Exception as exc:
                    _write_heartbeat(
                        cfg,
                        status="ERROR",
                        live=bool(args.live),
                        adapter=engine.adapter if engine is not None else None,
                        error=exc,
                    )
                    raise
                time.sleep(cfg.check_interval_seconds)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        _print({
            "status": "ERROR",
            "error": type(exc).__name__,
            "message": _sanitize_error_message(exc),
        })
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
