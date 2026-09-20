from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tapeout_harvester.signer import _secure_keyring


class SignerTests(unittest.TestCase):
    def fake_module(self, module_name: str):
        Backend = type("Backend", (), {})
        Backend.__module__ = module_name
        backend = Backend()
        return SimpleNamespace(get_keyring=lambda: backend)

    def test_plaintext_alt_backend_is_rejected(self):
        fake = self.fake_module("keyrings.alt.file")
        with patch.dict(sys.modules, {"keyring": fake}):
            with self.assertRaisesRegex(RuntimeError, "UNAPPROVED_KEYRING_BACKEND"):
                _secure_keyring()

    def test_macos_backend_is_allowed(self):
        fake = self.fake_module("keyring.backends.macOS")
        with patch.dict(sys.modules, {"keyring": fake}):
            self.assertIs(_secure_keyring(), fake)


if __name__ == "__main__":
    unittest.main()
