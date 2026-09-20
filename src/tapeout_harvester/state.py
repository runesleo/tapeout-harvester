from __future__ import annotations

import json
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - documented POSIX limitation
    fcntl = None


def default_state() -> dict[str, Any]:
    return {
        "schema": "tapeout-harvester-state/v2",
        "stage": "IDLE",
        "cycle_id": None,
        "cycle_identity_hash": None,
        "cycle_identity": None,
        "tx_hashes": {},
        "not_broadcast_hashes": {},
        "not_broadcast_label": None,
        "not_broadcast_reason": None,
        "tx_intent_at": {},
        "accepted_receipts": {},
        "gas_committed_wei": 0,
        "cleanup_reserve_wei": 0,
        "cleanup_gas_limit": None,
        "cleanup_gas_price_wei": None,
        "approval_created_by_cycle": False,
        "claimed_raw": None,
        "sell_raw": None,
        "received_raw": None,
        "cleanup_reason": None,
        "failure_reason": None,
        "last_completed_at": None,
        "last_receipt": None,
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "tapeout-harvester-state/v2":
        raise RuntimeError("UNSUPPORTED_STATE_SCHEMA")
    merged = default_state()
    merged.update(data)
    return merged


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_durable_directory(path: Path, *, private: bool = False) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent

    if current.exists():
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("RUNTIME_DIRECTORY_NOT_REAL_DIRECTORY")

    for directory in reversed(missing):
        directory.mkdir(mode=0o700 if private else 0o755)
        _fsync_directory(directory)
        _fsync_directory(directory.parent)

    if private:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("PRIVATE_LOCK_DIRECTORY_INVALID")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise RuntimeError("PRIVATE_LOCK_DIRECTORY_WRONG_OWNER")
        if info.st_mode & 0o077:
            os.chmod(path, 0o700)
            info = path.lstat()
            if info.st_mode & 0o077:
                raise RuntimeError("PRIVATE_LOCK_DIRECTORY_NOT_PRIVATE")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _ensure_durable_directory(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_directory(path.parent)


@contextmanager
def process_lock(path: Path, *, require_private_parent: bool = False) -> Iterator[None]:
    if fcntl is None:
        raise RuntimeError("POSIX_FCNTL_REQUIRED")

    _ensure_durable_directory(path.parent, private=require_private_parent)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError("LOCK_OPEN_FAILED") from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("LOCK_NOT_REGULAR_FILE")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise RuntimeError("LOCK_WRONG_OWNER")
        if info.st_nlink != 1:
            raise RuntimeError("LOCK_LINK_COUNT_INVALID")
        os.fchmod(fd, 0o600)

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("LOCK_HELD_ANOTHER_INSTANCE") from exc

        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()} at={time.time()}\n".encode())
        os.fsync(fd)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
