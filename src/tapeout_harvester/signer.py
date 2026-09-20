from __future__ import annotations

import getpass
import json
from pathlib import Path


_SECURE_BACKEND_PREFIXES = (
    "keyring.backends.macos.",
    "keyring.backends.windows.",
    "keyring.backends.secretservice.",
    "keyring.backends.kwallet.",
    "keyring.backends.libsecret.",
)


def _secure_keyring():
    try:
        import keyring  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError("KEYRING_EXTRA_REQUIRED") from exc
    backend = keyring.get_keyring()
    identity = f"{backend.__class__.__module__}.{backend.__class__.__name__}".lower()
    if not identity.startswith(_SECURE_BACKEND_PREFIXES):
        raise RuntimeError(f"UNAPPROVED_KEYRING_BACKEND:{backend.__class__.__name__}")
    return keyring


def store_keystore_password(service: str, account: str) -> None:
    password = getpass.getpass("Keystore password (stored only in approved OS keyring): ")
    if not password:
        raise RuntimeError("EMPTY_KEYSTORE_PASSWORD")
    _secure_keyring().set_password(service, account, password)


def load_local_account(keystore_path: Path, service: str, account: str):
    from eth_account import Account

    password = _secure_keyring().get_password(service, account)
    if password is None:
        raise RuntimeError("KEYSTORE_PASSWORD_NOT_FOUND_IN_OS_KEYRING")
    keyfile = json.loads(keystore_path.read_text(encoding="utf-8"))
    private_key = Account.decrypt(keyfile, password)
    return Account.from_key(private_key)
