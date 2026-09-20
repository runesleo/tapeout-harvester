from __future__ import annotations

import unittest
from types import SimpleNamespace

from tapeout_harvester.adapter import Web3TapeOutAdapter, _safe_rpc_label
from tapeout_harvester.cli import _sanitize_error_message


class HashAndRedactionTests(unittest.TestCase):
    def test_rpc_error_label_drops_credentials_and_path(self):
        label = _safe_rpc_label("https://user:secret@example.com/v3/private-key")
        self.assertEqual(label, "https://example.com")
        self.assertNotIn("secret", label)
        self.assertNotIn("private-key", label)

    def test_runtime_provider_error_is_fail_closed(self):
        msg = _sanitize_error_message(
            RuntimeError(
                "HTTPSConnectionPool(host='rpc.example.com'): "
                "Max retries exceeded with url: /v3/RELATIVE_SECRET?token=QUERY_SECRET"
            )
        )
        self.assertEqual(msg, "RUNTIME_DETAIL_REDACTED")
        self.assertNotIn("RELATIVE_SECRET", msg)
        self.assertNotIn("QUERY_SECRET", msg)
        self.assertNotIn("rpc.example.com", msg)

    def test_internal_short_error_code_is_preserved(self):
        self.assertEqual(
            _sanitize_error_message(RuntimeError("TX_NONCE_STALE_BEFORE_SIGN")),
            "TX_NONCE_STALE_BEFORE_SIGN",
        )

    def test_config_error_detail_is_not_echoed(self):
        msg = _sanitize_error_message(ValueError("network.rpc_urls contains /v3/SECRET"))
        self.assertEqual(msg, "CONFIG_INVALID")
        self.assertNotIn("SECRET", msg)

    def test_sign_normalizes_hash_and_rechecks_nonce(self):
        adapter = Web3TapeOutAdapter.__new__(Web3TapeOutAdapter)
        adapter.nonce = lambda: 1
        adapter.w3 = SimpleNamespace(
            keccak=lambda raw: SimpleNamespace(hex=lambda: "0x" + "ab" * 32)
        )
        account = SimpleNamespace(
            sign_transaction=lambda tx: SimpleNamespace(raw_transaction=b"signed")
        )
        signed = adapter.sign({"nonce": 1}, account)
        self.assertEqual(signed.tx_hash, "0x" + "ab" * 32)
        with self.assertRaisesRegex(RuntimeError, "NONCE_STALE"):
            adapter.sign({"nonce": 2}, account)


if __name__ == "__main__":
    unittest.main()
