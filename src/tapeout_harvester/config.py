from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def _dec(value: Any, name: str) -> Decimal:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not out.is_finite():
        raise ValueError(f"{name} must be finite")
    return out


def _bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a TOML boolean")
    return value


def _int(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be a TOML integer")
    return value


def _addr(value: Any, name: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 42 or not text.startswith("0x"):
        raise ValueError(f"{name} must be a 20-byte 0x address")
    try:
        number = int(text[2:], 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be hex") from exc
    if number == 0:
        raise ValueError(f"{name} cannot be the zero address")
    return text


def _bytes32(value: Any, name: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 66 or not text.startswith("0x"):
        raise ValueError(f"{name} must be a 32-byte 0x hex value")
    try:
        number = int(text[2:], 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be hex") from exc
    if number == 0:
        raise ValueError(f"{name} cannot be all zeroes")
    return text


@dataclass(frozen=True)
class HarvesterConfig:
    config_path: Path
    chain_id: int
    rpc_urls: tuple[str, ...]
    wallet: str
    mining_contract: str
    reward_token: str
    reward_decimals: int
    miner_keys: tuple[str, ...]
    minimum_claim: Decimal
    sell_fraction: Decimal
    check_interval_seconds: int
    router: str | None
    allowed_routers: tuple[str, ...]
    quoter: str | None
    pool: str | None
    fee: int | None
    destination_token: str | None
    destination_decimals: int | None
    unwrap_native: bool
    wrapped_native_token: str | None
    target_slippage_bps: int
    hard_slippage_bps: int
    quote_max_age_seconds: int
    gas_reserve_native: Decimal
    max_gas_native_per_cycle: Decimal
    min_confirmations: int
    receipt_dir: Path
    state_path: Path
    lock_path: Path
    live_enabled: bool
    keystore_path: Path | None
    keyring_service: str | None
    keyring_account: str | None
    tx_receipt_timeout_seconds: int

    @property
    def selling_enabled(self) -> bool:
        return self.sell_fraction > 0

    @property
    def wallet_lock_path(self) -> Path:
        key = hashlib.sha256(f"{self.chain_id}:{self.wallet.lower()}".encode()).hexdigest()[:24]
        return (
            Path.home()
            / ".local"
            / "state"
            / "tapeout-harvester"
            / "locks"
            / f"wallet-{key}.lock"
        )

    @property
    def heartbeat_path(self) -> Path:
        return self.state_path.with_name(f"{self.state_path.name}.heartbeat.json")

    @property
    def heartbeat_lock_path(self) -> Path:
        return self.heartbeat_path.with_suffix(self.heartbeat_path.suffix + ".lock")

    def cycle_identity(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "wallet": self.wallet,
            "mining_contract": self.mining_contract,
            "reward_token": self.reward_token,
            "reward_decimals": self.reward_decimals,
            "miner_keys": list(self.miner_keys),
            "minimum_claim": str(self.minimum_claim),
            "sell_fraction": str(self.sell_fraction),
            "router": self.router,
            "allowed_routers": list(self.allowed_routers),
            "quoter": self.quoter,
            "pool": self.pool,
            "fee": self.fee,
            "destination_token": self.destination_token,
            "destination_decimals": self.destination_decimals,
            "unwrap_native": self.unwrap_native,
            "wrapped_native_token": self.wrapped_native_token,
            "target_slippage_bps": self.target_slippage_bps,
            "hard_slippage_bps": self.hard_slippage_bps,
            "quote_max_age_seconds": self.quote_max_age_seconds,
            "gas_reserve_native": str(self.gas_reserve_native),
            "max_gas_native_per_cycle": str(self.max_gas_native_per_cycle),
            "min_confirmations": self.min_confirmations,
            "tx_receipt_timeout_seconds": self.tx_receipt_timeout_seconds,
        }

    def cycle_identity_hash(self) -> str:
        raw = json.dumps(self.cycle_identity(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()

    def watcher_identity(self) -> dict[str, Any]:
        return {
            "cycle_identity_hash": self.cycle_identity_hash(),
            "rpc_urls": list(self.rpc_urls),
            "check_interval_seconds": self.check_interval_seconds,
            "receipt_dir": str(self.receipt_dir),
            "state_path": str(self.state_path),
            "lock_path": str(self.lock_path),
            "live_enabled": self.live_enabled,
            "keystore_path": str(self.keystore_path) if self.keystore_path is not None else None,
            "keyring_service": self.keyring_service,
            "keyring_account": self.keyring_account,
        }

    def watcher_identity_hash(self) -> str:
        raw = json.dumps(
            self.watcher_identity(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def load(cls, path: str | Path) -> "HarvesterConfig":
        p = Path(path).expanduser().resolve()
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
        base = p.parent
        network = raw.get("network") or {}
        contracts = raw.get("contracts") or {}
        mining = raw.get("mining") or {}
        harvest = raw.get("harvest") or {}
        destination = raw.get("destination") or {}
        safety = raw.get("safety") or {}
        runtime = raw.get("runtime") or {}
        signer = raw.get("signer") or {}

        def rel(value: Any, default: str) -> Path:
            x = Path(str(value or default)).expanduser()
            return x.resolve() if x.is_absolute() else (base / x).resolve()

        def required(table: dict[str, Any], key: str, dotted: str) -> Any:
            if key not in table or table[key] in (None, ""):
                raise ValueError(f"{dotted} is required")
            return table[key]

        cfg = cls(
            config_path=p,
            chain_id=_int(network.get("chain_id", 56), "network.chain_id"),
            rpc_urls=tuple(str(x).strip() for x in network.get("rpc_urls", []) if str(x).strip()),
            wallet=_addr(network.get("wallet"), "network.wallet"),
            mining_contract=_addr(contracts.get("mining"), "contracts.mining"),
            reward_token=_addr(contracts.get("reward_token"), "contracts.reward_token"),
            reward_decimals=_int(contracts.get("reward_decimals", 8), "contracts.reward_decimals"),
            miner_keys=tuple(_bytes32(x, "mining.miner_keys[]") for x in mining.get("miner_keys", [])),
            minimum_claim=_dec(required(harvest, "minimum_claim", "harvest.minimum_claim"), "harvest.minimum_claim"),
            sell_fraction=_dec(harvest.get("sell_fraction", "0"), "harvest.sell_fraction"),
            check_interval_seconds=_int(harvest.get("check_interval_seconds", 900), "harvest.check_interval_seconds"),
            router=_addr(contracts.get("router"), "contracts.router") if contracts.get("router") else None,
            allowed_routers=tuple(_addr(x, "contracts.allowed_routers[]") for x in contracts.get("allowed_routers", [])),
            quoter=_addr(contracts.get("quoter"), "contracts.quoter") if contracts.get("quoter") else None,
            pool=_addr(contracts.get("pool"), "contracts.pool") if contracts.get("pool") else None,
            fee=_int(contracts["fee"], "contracts.fee") if contracts.get("fee") is not None else None,
            destination_token=_addr(destination.get("token"), "destination.token") if destination.get("token") else None,
            destination_decimals=_int(destination["decimals"], "destination.decimals") if destination.get("decimals") is not None else None,
            unwrap_native=_bool(destination.get("unwrap_native", False), "destination.unwrap_native"),
            wrapped_native_token=_addr(destination.get("wrapped_native_token"), "destination.wrapped_native_token") if destination.get("wrapped_native_token") else None,
            target_slippage_bps=_int(required(safety, "target_slippage_bps", "safety.target_slippage_bps"), "safety.target_slippage_bps"),
            hard_slippage_bps=_int(required(safety, "hard_slippage_bps", "safety.hard_slippage_bps"), "safety.hard_slippage_bps"),
            quote_max_age_seconds=_int(required(safety, "quote_max_age_seconds", "safety.quote_max_age_seconds"), "safety.quote_max_age_seconds"),
            gas_reserve_native=_dec(required(safety, "gas_reserve_native", "safety.gas_reserve_native"), "safety.gas_reserve_native"),
            max_gas_native_per_cycle=_dec(required(safety, "max_gas_native_per_cycle", "safety.max_gas_native_per_cycle"), "safety.max_gas_native_per_cycle"),
            min_confirmations=_int(required(safety, "min_confirmations", "safety.min_confirmations"), "safety.min_confirmations"),
            receipt_dir=rel(runtime.get("receipt_dir"), "./receipts"),
            state_path=rel(runtime.get("state_path"), "./state/state.json"),
            lock_path=rel(runtime.get("lock_path"), "./state/harvester.lock"),
            live_enabled=_bool(runtime.get("live_enabled", False), "runtime.live_enabled"),
            keystore_path=rel(signer.get("keystore_path"), "./wallet.json") if signer.get("keystore_path") else None,
            keyring_service=str(signer.get("keyring_service")) if signer.get("keyring_service") else None,
            keyring_account=str(signer.get("keyring_account")) if signer.get("keyring_account") else None,
            tx_receipt_timeout_seconds=_int(safety.get("tx_receipt_timeout_seconds", 120), "safety.tx_receipt_timeout_seconds"),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.chain_id <= 0:
            raise ValueError("network.chain_id must be positive")
        if not self.rpc_urls:
            raise ValueError("network.rpc_urls cannot be empty")
        if not self.miner_keys:
            raise ValueError("mining.miner_keys cannot be empty")
        if not 0 <= self.reward_decimals <= 36:
            raise ValueError("contracts.reward_decimals out of range")
        if self.minimum_claim < 0:
            raise ValueError("harvest.minimum_claim cannot be negative")
        if not Decimal("0") <= self.sell_fraction <= Decimal("1"):
            raise ValueError("harvest.sell_fraction must be between 0 and 1")
        if self.check_interval_seconds < 30:
            raise ValueError("harvest.check_interval_seconds must be >= 30")
        if not (0 <= self.target_slippage_bps <= self.hard_slippage_bps <= 2_000):
            raise ValueError("slippage bps must satisfy 0 <= target <= hard <= 2000")
        if self.quote_max_age_seconds <= 0:
            raise ValueError("safety.quote_max_age_seconds must be positive")
        if self.gas_reserve_native < 0 or self.max_gas_native_per_cycle <= 0:
            raise ValueError("gas limits must be positive")
        if self.tx_receipt_timeout_seconds <= 0:
            raise ValueError("safety.tx_receipt_timeout_seconds must be positive")
        if self.min_confirmations <= 0:
            raise ValueError("safety.min_confirmations must be positive")

        state_tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp").resolve()
        heartbeat = self.heartbeat_path.resolve()
        heartbeat_tmp = heartbeat.with_suffix(heartbeat.suffix + ".tmp")
        files: dict[str, Path] = {
            "config": self.config_path.resolve(),
            "state": self.state_path.resolve(),
            "state_tmp": state_tmp,
            "heartbeat": heartbeat,
            "heartbeat_tmp": heartbeat_tmp,
            "heartbeat_lock": self.heartbeat_lock_path.resolve(),
            "lock": self.lock_path.resolve(),
            "wallet_lock": self.wallet_lock_path.resolve(),
        }
        if self.keystore_path is not None:
            files["keystore"] = self.keystore_path.resolve()
        inverse: dict[Path, str] = {}
        for name, value in files.items():
            if value in inverse:
                raise ValueError(f"runtime path collision: {name} == {inverse[value]}")
            inverse[value] = name
        existing = [(name, value) for name, value in files.items() if value.exists()]
        for index, (left_name, left_path) in enumerate(existing):
            for right_name, right_path in existing[index + 1:]:
                try:
                    if os.path.samefile(left_path, right_path):
                        raise ValueError(f"runtime inode collision: {left_name} == {right_name}")
                except FileNotFoundError:
                    pass
        receipt_root = self.receipt_dir.resolve()
        for name, value in files.items():
            if value == receipt_root or value.is_relative_to(receipt_root):
                raise ValueError(f"runtime path collision: {name} is inside receipt_dir")
        if receipt_root == state_tmp or receipt_root.is_relative_to(state_tmp):
            raise ValueError("runtime path collision: receipt_dir conflicts with state temp path")

        if self.selling_enabled:
            required = {
                "contracts.router": self.router,
                "contracts.quoter": self.quoter,
                "contracts.pool": self.pool,
                "contracts.fee": self.fee,
                "destination.token": self.destination_token,
                "destination.decimals": self.destination_decimals,
            }
            missing = [k for k, v in required.items() if v is None]
            if missing:
                raise ValueError("selling enabled but missing: " + ", ".join(missing))
            if int(self.fee or 0) <= 0 or int(self.fee or 0) >= 2**24:
                raise ValueError("contracts.fee must be a positive uint24 value")
            if not self.allowed_routers or self.router not in self.allowed_routers:
                raise ValueError("contracts.router must appear in contracts.allowed_routers")
            if self.destination_decimals is None or not 0 <= self.destination_decimals <= 36:
                raise ValueError("destination.decimals out of range")
            if self.unwrap_native and self.wrapped_native_token != self.destination_token:
                raise ValueError("unwrap_native requires destination.token == wrapped_native_token")
