from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tapeout_harvester.config import HarvesterConfig


BASE = """
[network]
chain_id = 56
rpc_urls = ["https://example.invalid"]
wallet = "0x1111111111111111111111111111111111111111"
[contracts]
mining = "0x2222222222222222222222222222222222222222"
reward_token = "0x3333333333333333333333333333333333333333"
reward_decimals = 8
[mining]
miner_keys = ["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
[harvest]
minimum_claim = "1"
sell_fraction = "0"
check_interval_seconds = 60
[safety]
target_slippage_bps = 25
hard_slippage_bps = 75
quote_max_age_seconds = 15
gas_reserve_native = "0.01"
max_gas_native_per_cycle = "0.001"
tx_receipt_timeout_seconds = 120
min_confirmations = 1
[runtime]
live_enabled = false
"""


class ConfigTests(unittest.TestCase):
    def load(self, text=BASE):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.toml"
            p.write_text(text)
            return HarvesterConfig.load(p)

    def test_claim_only_minimal_config_is_valid(self):
        cfg = self.load()
        self.assertFalse(cfg.selling_enabled)
        self.assertFalse(cfg.live_enabled)
        self.assertEqual(cfg.min_confirmations, 1)

    def test_zero_wallet_rejected(self):
        text = BASE.replace(
            "0x1111111111111111111111111111111111111111",
            "0x0000000000000000000000000000000000000000",
        )
        with self.assertRaisesRegex(ValueError, "zero address"):
            self.load(text)

    def test_zero_miner_key_rejected(self):
        text = BASE.replace("0x" + "aa" * 32, "0x" + "00" * 32)
        with self.assertRaisesRegex(ValueError, "all zeroes"):
            self.load(text)

    def test_missing_minimum_claim_rejected(self):
        with self.assertRaisesRegex(ValueError, "minimum_claim is required"):
            self.load(BASE.replace('minimum_claim = "1"\n', ""))

    def test_missing_explicit_safety_budget_rejected(self):
        with self.assertRaisesRegex(ValueError, "gas_reserve_native is required"):
            self.load(BASE.replace('gas_reserve_native = "0.01"\n', ""))

    def test_missing_confirmation_policy_rejected(self):
        with self.assertRaisesRegex(ValueError, "min_confirmations is required"):
            self.load(BASE.replace("min_confirmations = 1\n", ""))

    def test_string_false_cannot_enable_live_mode(self):
        text = BASE.replace("live_enabled = false", 'live_enabled = "false"')
        with self.assertRaisesRegex(ValueError, "TOML boolean"):
            self.load(text)

    def test_string_false_cannot_enable_unwrap(self):
        text = BASE + """
[destination]
unwrap_native = "false"
"""
        with self.assertRaisesRegex(ValueError, "TOML boolean"):
            self.load(text)

    def test_sell_requires_route_and_destination(self):
        text = BASE.replace('sell_fraction = "0"', 'sell_fraction = "1"')
        with self.assertRaisesRegex(ValueError, "selling enabled but missing"):
            self.load(text)

    def test_sell_router_must_be_allowlisted(self):
        extra = """
router = "0x4444444444444444444444444444444444444444"
allowed_routers = ["0x9999999999999999999999999999999999999999"]
quoter = "0x5555555555555555555555555555555555555555"
pool = "0x6666666666666666666666666666666666666666"
fee = 2500
"""
        text = (
            BASE.replace("reward_decimals = 8\n", "reward_decimals = 8\n" + extra)
            .replace('sell_fraction = "0"', 'sell_fraction = "1"')
            + """
[destination]
token = "0x7777777777777777777777777777777777777777"
decimals = 18
unwrap_native = false
"""
        )
        with self.assertRaisesRegex(ValueError, "allowed_routers"):
            self.load(text)

    def test_check_interval_has_floor(self):
        with self.assertRaisesRegex(ValueError, ">= 30"):
            self.load(BASE.replace("check_interval_seconds = 60", "check_interval_seconds = 5"))

    def test_safety_integer_rejects_float_and_bool(self):
        with self.assertRaisesRegex(ValueError, "TOML integer"):
            self.load(BASE.replace("min_confirmations = 1", "min_confirmations = 1.9"))
        with self.assertRaisesRegex(ValueError, "TOML integer"):
            self.load(BASE.replace("min_confirmations = 1", "min_confirmations = true"))

    def test_wallet_lock_collision_is_rejected(self):
        cfg = self.load()
        with self.assertRaisesRegex(ValueError, "path collision"):
            replace(cfg, state_path=cfg.wallet_lock_path).validate()

    def test_receipt_dir_cannot_alias_state_temp_path(self):
        cfg = self.load()
        state_tmp = cfg.state_path.with_suffix(cfg.state_path.suffix + ".tmp")
        with self.assertRaisesRegex(ValueError, "path collision"):
            replace(cfg, receipt_dir=state_tmp).validate()

    def test_state_and_lock_path_collision_rejected(self):
        text = BASE + """
[runtime.extra]
unused = true
"""
        # Replace the existing runtime table instead of defining it twice.
        text = BASE.replace(
            "[runtime]\nlive_enabled = false",
            '[runtime]\nlive_enabled = false\nstate_path = "./state/shared"\nlock_path = "./state/shared"',
        )
        with self.assertRaisesRegex(ValueError, "path collision"):
            self.load(text)


if __name__ == "__main__":
    unittest.main()
