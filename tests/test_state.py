from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tapeout_harvester.state import atomic_write_json, load_state, process_lock


class StateTests(unittest.TestCase):
    def test_atomic_write_fsyncs_file_new_directory_and_parent(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state/state.json"
            with patch("tapeout_harvester.state.os.fsync") as fsync:
                atomic_write_json(
                    path,
                    {"schema": "tapeout-harvester-state/v2", "stage": "IDLE"},
                )
            self.assertGreaterEqual(fsync.call_count, 4)
            self.assertEqual(json.loads(path.read_text())["stage"], "IDLE")

    def test_v1_state_is_rejected_instead_of_silent_migration(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text(
                '{"schema":"tapeout-harvester-state/v1","stage":"CLAIM_INTENT"}'
            )
            with self.assertRaisesRegex(RuntimeError, "UNSUPPORTED_STATE_SCHEMA"):
                load_state(path)

    def test_lock_symlink_is_rejected_without_truncating_target(self):
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target.txt"
            target.write_text("keep-me")
            link = root / "lock"
            link.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "LOCK_OPEN_FAILED"):
                with process_lock(link):
                    pass
            self.assertEqual(target.read_text(), "keep-me")

    def test_hardlinked_lock_is_rejected_without_truncation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = root / "original.lock"
            alias = root / "alias.lock"
            original.write_text("keep-me")
            os.link(original, alias)
            with self.assertRaisesRegex(RuntimeError, "LINK_COUNT"):
                with process_lock(alias):
                    pass
            self.assertEqual(original.read_text(), "keep-me")

    def test_private_lock_parent_is_owner_only(self):
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td) / "nested/private"
            lock = parent / "wallet.lock"
            with process_lock(lock, require_private_parent=True):
                mode = parent.stat().st_mode & 0o777
                self.assertEqual(mode, 0o700)
                self.assertEqual(lock.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
