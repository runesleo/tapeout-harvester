from __future__ import annotations

import argparse
import json
import re
import time

from .adapter import Web3TapeOutAdapter
from .config import HarvesterConfig
from .engine import HarvesterEngine
from .signer import load_local_account, store_keystore_password
from .state import load_state


_INTERNAL_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*(?::[A-Za-z0-9_.-]+)?$")


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


def main() -> int:
    p = argparse.ArgumentParser(prog="tapeout-harvester")
    p.add_argument("--config", default="config.toml")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate config and print a sanitized summary")
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
        if args.command == "signer":
            if not (cfg.keyring_service and cfg.keyring_account):
                raise RuntimeError("KEYRING_SERVICE_AND_ACCOUNT_REQUIRED")
            store_keystore_password(cfg.keyring_service, cfg.keyring_account)
            _print({"status": "PASSWORD_STORED_IN_SECURE_KEYRING"})
            return 0
        engine = _engine(cfg)
        if args.command == "run":
            result = engine.run(live=bool(args.live))
            _print(result.to_dict())
            return 0 if result.status in {
                "SKIP", "DRY_RUN_READY", "COMPLETE", "PENDING_TX", "PENDING_CONFIRMATIONS"
            } else 2
        if args.command == "watch":
            while True:
                result = engine.run(live=bool(args.live))
                _print(result.to_dict())
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
