from __future__ import annotations

import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from tapeout_harvester.adapter import Web3TapeOutAdapter, _hex
from tapeout_harvester.models import Quote

try:
    from web3 import Web3
    HAS_WEB3 = True
except ImportError:
    Web3 = None
    HAS_WEB3 = False


def topic_address(address: str) -> str:
    return "0x" + ("00" * 12) + address[2:].lower()


def data_uint(value: int) -> str:
    return "0x" + int(value).to_bytes(32, "big").hex()


@unittest.skipUnless(HAS_WEB3, "web3 dependency is not installed in this interpreter")
class AdapterDependencyTests(unittest.TestCase):
    def blank_adapter(self):
        adapter = Web3TapeOutAdapter.__new__(Web3TapeOutAdapter)
        adapter._transfer_topic = _hex(Web3.keccak(text="Transfer(address,address,uint256)"))
        adapter._withdrawal_topic = _hex(Web3.keccak(text="Withdrawal(address,uint256)"))
        return adapter

    def test_selects_rpc_only_after_representative_pending_read(self):
        class FakeCall:
            def __init__(self, provider):
                self.provider = provider

            def call(self, *_args, **_kwargs):
                if "bad.example" in self.provider:
                    raise OSError("missing trie node")
                return 7

        class FakeFunctions:
            def __init__(self, provider):
                self.provider = provider

            def pending(self, _key):
                return FakeCall(self.provider)

        class FakeContract:
            def __init__(self, provider, address):
                self.address = address
                self.functions = FakeFunctions(provider)

        class FakeEth:
            chain_id = 56
            block_number = 123

            def __init__(self, provider):
                self.provider = provider

            def contract(self, address, abi):
                return FakeContract(self.provider, address)

        class FakeWeb3:
            def __init__(self, provider):
                self.provider = provider
                self.eth = FakeEth(provider)
                self.middleware_onion = SimpleNamespace(inject=lambda *_args, **_kwargs: None)

            @staticmethod
            def HTTPProvider(url, request_kwargs=None):
                return url

            @staticmethod
            def to_checksum_address(address):
                return address

            @staticmethod
            def keccak(text=None):
                return bytes.fromhex("11" * 32)

        cfg = SimpleNamespace(
            chain_id=56,
            rpc_urls=("https://bad.example", "https://good.example"),
            wallet="0x1111111111111111111111111111111111111111",
            mining_contract="0x2222222222222222222222222222222222222222",
            reward_token="0x3333333333333333333333333333333333333333",
            miner_keys=("0x" + "aa" * 32,),
            selling_enabled=False,
        )
        with patch("web3.Web3", FakeWeb3):
            adapter = Web3TapeOutAdapter(cfg)
        self.assertEqual(adapter.rpc_url, "https://good.example")
        self.assertEqual(adapter.rpc_label, "https://good.example")

    def test_decodes_transfer_and_withdrawal_events(self):
        adapter = self.blank_adapter()
        token = "0x3333333333333333333333333333333333333333"
        wallet = "0x1111111111111111111111111111111111111111"
        sender = "0x2222222222222222222222222222222222222222"
        logs = [
            {
                "address": token,
                "topics": [
                    adapter._transfer_topic,
                    topic_address(sender),
                    topic_address(wallet),
                ],
                "data": data_uint(123456789),
            },
            {
                "address": token,
                "topics": [
                    adapter._withdrawal_topic,
                    topic_address(wallet),
                ],
                "data": data_uint(987654321),
            },
        ]
        transfers, withdrawals = adapter._decode_logs(logs)
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0].recipient, wallet)
        self.assertEqual(transfers[0].sender, sender)
        self.assertEqual(transfers[0].amount_raw, 123456789)
        self.assertEqual(len(withdrawals), 1)
        self.assertEqual(withdrawals[0].owner, wallet)
        self.assertEqual(withdrawals[0].amount_raw, 987654321)

    def test_get_receipt_rejects_noncanonical_block_and_counts_confirmations(self):
        adapter = self.blank_adapter()
        canonical_hash = "0x" + "ab" * 32
        token = "0x3333333333333333333333333333333333333333"
        wallet = "0x1111111111111111111111111111111111111111"
        receipt = {
            "status": 1,
            "gasUsed": 21000,
            "effectiveGasPrice": 1_000_000_000,
            "blockNumber": 100,
            "blockHash": canonical_hash,
            "logs": [{
                "address": token,
                "topics": [
                    adapter._transfer_topic,
                    topic_address("0x2222222222222222222222222222222222222222"),
                    topic_address(wallet),
                ],
                "data": data_uint(42),
            }],
        }

        class Eth:
            block_number = 102
            def get_transaction_receipt(self, _tx_hash):
                return receipt
            def get_block(self, _block_number):
                return {"hash": canonical_hash}

        adapter.w3 = SimpleNamespace(eth=Eth())
        out = adapter.get_receipt("0x" + "01" * 32)
        self.assertEqual(out.confirmations, 3)
        self.assertEqual(out.received_raw(token, wallet), 42)

        class ReorgEth(Eth):
            def get_block(self, _block_number):
                return {"hash": "0x" + "cd" * 32}

        adapter.w3 = SimpleNamespace(eth=ReorgEth())
        self.assertIsNone(adapter.get_receipt("0x" + "01" * 32))

    def test_hard_slippage_floor_caps_total_spot_deviation(self):
        adapter = self.blank_adapter()
        reward = "0x3333333333333333333333333333333333333333"
        dest = "0x7777777777777777777777777777777777777777"
        wallet = "0x1111111111111111111111111111111111111111"
        adapter.cfg = SimpleNamespace(
            reward_token=reward,
            destination_token=dest,
            reward_decimals=18,
            destination_decimals=18,
            target_slippage_bps=25,
            hard_slippage_bps=75,
            fee=2500,
        )
        adapter.wallet = wallet
        adapter.reward = SimpleNamespace(address=reward)
        adapter.dest = SimpleNamespace(address=dest)

        class Call:
            def __init__(self, value):
                self.value = value
            def call(self, *_args, **_kwargs):
                return self.value

        class QuoterFns:
            def quoteExactInputSingle(self, _params):
                # Quote is 70 bps below a pool spot of 1.0.
                return Call([993_000_000_000_000_000, 0, 0, 0])

        class PoolFns:
            def token0(self): return Call(reward)
            def token1(self): return Call(dest)
            def slot0(self): return Call([2**96, 0, 0, 0, 0, 0, True])

        adapter.quoter = SimpleNamespace(functions=QuoterFns())
        adapter.pool = SimpleNamespace(functions=PoolFns())
        quote = adapter.quote(10**18)
        self.assertEqual(quote.deviation_bps, Decimal("70.000"))
        # 75 bps hard floor from spot = 0.9925, stricter than 25 bps off the 0.993 quote.
        self.assertEqual(quote.min_out_raw, 992_500_000_000_000_000)

    def test_swap_build_rejects_expired_quote_before_router_access(self):
        adapter = self.blank_adapter()
        adapter.cfg = SimpleNamespace(quote_max_age_seconds=15)
        quote = Quote(
            amount_in_raw=1,
            amount_out_raw=1,
            min_out_raw=1,
            quoted_at=time.time() - 16,
            execution_price=Decimal("1"),
            spot_price=Decimal("1"),
            deviation_bps=Decimal("0"),
        )
        with self.assertRaisesRegex(RuntimeError, "QUOTE_STALE_BEFORE_SWAP_BUILD"):
            adapter.swap_tx(quote)


if __name__ == "__main__":
    unittest.main()
