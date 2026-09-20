from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SecurityPreflightTests(unittest.TestCase):
    def test_secret_finding_never_echoes_secret_value(self):
        repo_root = Path(__file__).resolve().parents[1]
        script = repo_root / "scripts/security_preflight.py"
        secret = "sk_" + ("A" * 24)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sample.toml").write_text(f'api_token = "{secret}"\n')
            result = subprocess.run(
                [sys.executable, str(script), str(root)],
                text=True,
                capture_output=True,
                check=False,
            )
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SECRET_LIKE_PATTERN", output)
        self.assertNotIn(secret, output)
        self.assertIn("sample.toml:1", output)


if __name__ == "__main__":
    unittest.main()
