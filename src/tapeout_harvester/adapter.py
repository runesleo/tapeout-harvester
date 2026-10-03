from __future__ import annotations

import time
from decimal import Decimal, getcontext
from typing import Any
from urllib.parse import urlsplit

from .config import HarvesterConfig
from .models import NativeWithdrawal, Quote, SignedTx, TokenTransfer, TxReceipt

getcontext().prec = 60


def _safe_rpc_label(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = parts.hostname or "unknown-host"
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme or 'rpc'}://{host}{port}"
    except Exception:
        return "rpc://redacted"


def _hex(value: Any) -> str:
    if value is None:
        return "0x"
    if isinstance(value, str):
        text = value
    elif isinstance(value, (bytes, bytearray)):
        text = bytes(value).hex()
    elif hasattr(value, "hex"):
        text = value.hex()
    else:
        text = str(value)
    text = str(text).lower()
    return text if text.startswith("0x") else "0x" + text


def _topic_address(value: Any) -> str:
    text = _hex(value)
    if len(text) < 42:
        raise RuntimeError("MALFORMED_EVENT_TOPIC_ADDRESS")
    return "0x" + text[-40:]


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


MINING_ABI = [
    {"type": "function", "name": "claimMany", "stateMutability": "nonpayable", "inputs": [{"name": "keys", "type": "bytes32[]"}], "outputs": []},
    {"type": "function", "name": "pending", "stateMutability": "view", "inputs": [{"name": "key", "type": "bytes32"}], "outputs": [{"name": "", "type": "uint256"}]},
]
ERC20_ABI = [
    {"type": "function", "name": "balanceOf", "stateMutability": "view", "inputs": [{"name": "account", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "allowance", "stateMutability": "view", "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "approve", "stateMutability": "nonpayable", "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "outputs": [{"name": "", "type": "bool"}]},
]
QUOTER_ABI = [{
    "type": "function", "name": "quoteExactInputSingle", "stateMutability": "nonpayable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "fee", "type": "uint24"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}
    ]}],
    "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "sqrtPriceX96After", "type": "uint160"}, {"name": "initializedTicksCrossed", "type": "uint32"}, {"name": "gasEstimate", "type": "uint256"}]
}]
ROUTER_ABI = [{
    "type": "function", "name": "exactInputSingle", "stateMutability": "payable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "fee", "type": "uint24"}, {"name": "recipient", "type": "address"},
        {"name": "deadline", "type": "uint256"}, {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMinimum", "type": "uint256"}, {"name": "sqrtPriceLimitX96", "type": "uint160"}
    ]}], "outputs": [{"name": "amountOut", "type": "uint256"}]
}]
POOL_ABI = [
    {"type": "function", "name": "token0", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"type": "function", "name": "token1", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"type": "function", "name": "slot0", "stateMutability": "view", "inputs": [], "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
        {"name": "observationIndex", "type": "uint16"}, {"name": "observationCardinality", "type": "uint16"},
        {"name": "observationCardinalityNext", "type": "uint16"}, {"name": "feeProtocol", "type": "uint32"},
        {"name": "unlocked", "type": "bool"}
    ]},
]
WNATIVE_ABI = [{"type": "function", "name": "withdraw", "stateMutability": "nonpayable", "inputs": [{"name": "wad", "type": "uint256"}], "outputs": []}]


class Web3TapeOutAdapter:
    def __init__(self, config: HarvesterConfig):
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware

        self.cfg = config
        errors: list[str] = []
        self.w3 = None
        for url in config.rpc_urls:
            try:
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 12}))
                w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                if int(w3.eth.chain_id) != config.chain_id:
                    raise RuntimeError("CHAIN_ID_MISMATCH")
                int(w3.eth.block_number)
                # A node can answer chainId/blockNumber while its current state trie
                # is unhealthy. Probe the exact TapeOut read path before selecting it.
                probe_mining = w3.eth.contract(
                    address=Web3.to_checksum_address(config.mining_contract),
                    abi=MINING_ABI,
                )
                probe_key = bytes.fromhex(config.miner_keys[0][2:])
                int(probe_mining.functions.pending(probe_key).call())
                self.w3 = w3
                self.rpc_url = url
                break
            except Exception as exc:
                errors.append(f"{_safe_rpc_label(url)}: {type(exc).__name__}")
        if self.w3 is None:
            raise RuntimeError("RPC_UNAVAILABLE: " + "; ".join(errors))

        self.wallet = Web3.to_checksum_address(config.wallet)
        self.mining = self.w3.eth.contract(address=Web3.to_checksum_address(config.mining_contract), abi=MINING_ABI)
        self.reward = self.w3.eth.contract(address=Web3.to_checksum_address(config.reward_token), abi=ERC20_ABI)
        if config.selling_enabled:
            self.dest = self.w3.eth.contract(address=Web3.to_checksum_address(config.destination_token), abi=ERC20_ABI)
            self.router = self.w3.eth.contract(address=Web3.to_checksum_address(config.router), abi=ROUTER_ABI)
            self.quoter = self.w3.eth.contract(address=Web3.to_checksum_address(config.quoter), abi=QUOTER_ABI)
            self.pool = self.w3.eth.contract(address=Web3.to_checksum_address(config.pool), abi=POOL_ABI)

        self._transfer_topic = _hex(self.w3.keccak(text="Transfer(address,address,uint256)"))
        self._withdrawal_topic = _hex(self.w3.keccak(text="Withdrawal(address,uint256)"))

    @property
    def rpc_label(self) -> str:
        return _safe_rpc_label(self.rpc_url)

    def pending_raw(self) -> int:
        return sum(int(self.mining.functions.pending(bytes.fromhex(key[2:])).call()) for key in self.cfg.miner_keys)

    def reward_balance_raw(self) -> int:
        return int(self.reward.functions.balanceOf(self.wallet).call())

    def destination_balance_raw(self) -> int:
        if not self.cfg.selling_enabled:
            return 0
        return int(self.dest.functions.balanceOf(self.wallet).call())

    def native_balance_wei(self) -> int:
        return int(self.w3.eth.get_balance(self.wallet))

    def gas_price_wei(self) -> int:
        return int(self.w3.eth.gas_price)

    def nonce(self) -> int:
        latest = int(self.w3.eth.get_transaction_count(self.wallet, "latest"))
        pending = int(self.w3.eth.get_transaction_count(self.wallet, "pending"))
        if pending != latest:
            raise RuntimeError("NONCE_GAP_OR_INFLIGHT_TX")
        return latest

    def _base(self) -> dict[str, Any]:
        return {"from": self.wallet, "nonce": self.nonce(), "chainId": self.cfg.chain_id}

    def claim_tx(self) -> dict[str, Any]:
        fn = self.mining.functions.claimMany([bytes.fromhex(key[2:]) for key in self.cfg.miner_keys])
        base = self._base()
        fn.call({"from": self.wallet})
        gas = int(fn.estimate_gas({"from": self.wallet}))
        return fn.build_transaction({**base, "gas": int(gas * 1.20), "gasPrice": self.gas_price_wei()})

    def allowance_raw(self) -> int:
        return int(self.reward.functions.allowance(self.wallet, self.router.address).call())

    def approve_tx(
        self,
        amount_raw: int,
        *,
        gas_limit: int | None = None,
        gas_price_wei: int | None = None,
    ) -> dict[str, Any]:
        fn = self.reward.functions.approve(self.router.address, int(amount_raw))
        fn.call({"from": self.wallet})
        gas = int(gas_limit) if gas_limit is not None else int(int(fn.estimate_gas({"from": self.wallet})) * 1.20)
        gas_price = int(gas_price_wei) if gas_price_wei is not None else self.gas_price_wei()
        return fn.build_transaction({**self._base(), "gas": gas, "gasPrice": gas_price})

    def _spot_price(self) -> Decimal:
        token0 = self.pool.functions.token0().call().lower()
        token1 = self.pool.functions.token1().call().lower()
        reward = self.cfg.reward_token.lower()
        dest = self.cfg.destination_token.lower()
        if {token0, token1} != {reward, dest}:
            raise RuntimeError("POOL_TOKEN_MISMATCH")
        slot0 = self.pool.functions.slot0().call()
        sqrt_x96 = Decimal(int(slot0[0]))
        raw_1_per_0 = (sqrt_x96 * sqrt_x96) / Decimal(2**192)
        if token0 == reward:
            return raw_1_per_0 * (Decimal(10) ** (self.cfg.reward_decimals - int(self.cfg.destination_decimals)))
        return (Decimal(1) / raw_1_per_0) * (Decimal(10) ** (self.cfg.reward_decimals - int(self.cfg.destination_decimals)))

    def quote(self, amount_in_raw: int) -> Quote:
        quoted_at = time.time()
        params = (self.reward.address, self.dest.address, int(amount_in_raw), int(self.cfg.fee), 0)
        result = self.quoter.functions.quoteExactInputSingle(params).call({"from": self.wallet})
        amount_out = int(result[0])
        if amount_out <= 0:
            raise RuntimeError("QUOTE_ZERO")

        human_in = Decimal(amount_in_raw) / (Decimal(10) ** self.cfg.reward_decimals)
        human_out = Decimal(amount_out) / (Decimal(10) ** int(self.cfg.destination_decimals))
        execution_price = human_out / human_in
        spot = self._spot_price()
        if spot <= 0:
            raise RuntimeError("POOL_SPOT_INVALID")
        deviation = abs(execution_price / spot - Decimal(1)) * Decimal(10_000)

        target_min = int(
            Decimal(amount_out)
            * (Decimal(10_000 - self.cfg.target_slippage_bps) / Decimal(10_000))
        )
        hard_human_out = (
            human_in
            * spot
            * (Decimal(10_000 - self.cfg.hard_slippage_bps) / Decimal(10_000))
        )
        hard_min = int(hard_human_out * (Decimal(10) ** int(self.cfg.destination_decimals)))
        min_out = max(target_min, hard_min)
        return Quote(amount_in_raw, amount_out, min_out, quoted_at, execution_price, spot, deviation)

    def swap_tx(self, quote: Quote) -> dict[str, Any]:
        deadline = int(quote.quoted_at + self.cfg.quote_max_age_seconds)
        if time.time() >= deadline:
            raise RuntimeError("QUOTE_STALE_BEFORE_SWAP_BUILD")
        params = (
            self.reward.address, self.dest.address, int(self.cfg.fee), self.wallet, deadline,
            int(quote.amount_in_raw), int(quote.min_out_raw), 0,
        )
        fn = self.router.functions.exactInputSingle(params)
        fn.call({"from": self.wallet, "value": 0})
        gas = int(fn.estimate_gas({"from": self.wallet, "value": 0}))
        return fn.build_transaction({**self._base(), "gas": int(gas * 1.20), "gasPrice": self.gas_price_wei(), "value": 0})

    def unwrap_tx(self, amount_raw: int) -> dict[str, Any]:
        from web3 import Web3

        wrapped = self.w3.eth.contract(address=Web3.to_checksum_address(self.cfg.wrapped_native_token), abi=WNATIVE_ABI)
        fn = wrapped.functions.withdraw(int(amount_raw))
        fn.call({"from": self.wallet})
        gas = int(fn.estimate_gas({"from": self.wallet}))
        return fn.build_transaction({**self._base(), "gas": int(gas * 1.20), "gasPrice": self.gas_price_wei()})

    def estimate_native_cost(self, *txs: dict[str, Any]) -> Decimal:
        wei = sum(
            int(tx.get("gas", 0)) * int(tx.get("gasPrice", self.gas_price_wei()))
            for tx in txs
        )
        return Decimal(wei) / Decimal(10**18)

    def sign(self, tx: dict[str, Any], account) -> SignedTx:
        if int(tx.get("nonce", -1)) != self.nonce():
            raise RuntimeError("TX_NONCE_STALE_BEFORE_SIGN")
        signed = account.sign_transaction(tx)
        raw = bytes(signed.raw_transaction)
        tx_hash = _hex(self.w3.keccak(raw))
        return SignedTx(tx_hash, raw)

    def broadcast(self, signed: SignedTx) -> str:
        sent = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        actual = _hex(sent)
        expected = signed.tx_hash.lower()
        if actual != expected:
            raise RuntimeError("TX_HASH_MISMATCH_AFTER_BROADCAST")
        return actual

    def _decode_logs(self, logs: list[Any]) -> tuple[tuple[TokenTransfer, ...], tuple[NativeWithdrawal, ...]]:
        transfers: list[TokenTransfer] = []
        withdrawals: list[NativeWithdrawal] = []
        for log in logs:
            topics = list(_field(log, "topics", []) or [])
            if not topics:
                continue
            topic0 = _hex(topics[0])
            address = str(_field(log, "address", "")).lower()
            data_hex = _hex(_field(log, "data", "0x0"))
            amount = int(data_hex, 16) if data_hex not in {"0x", "0x0"} else 0

            if topic0 == self._transfer_topic and len(topics) >= 3:
                transfers.append(TokenTransfer(
                    token=address,
                    sender=_topic_address(topics[1]),
                    recipient=_topic_address(topics[2]),
                    amount_raw=amount,
                ))
            elif topic0 == self._withdrawal_topic and len(topics) >= 2:
                withdrawals.append(NativeWithdrawal(
                    token=address,
                    owner=_topic_address(topics[1]),
                    amount_raw=amount,
                ))
        return tuple(transfers), tuple(withdrawals)

    def get_receipt(self, tx_hash: str) -> TxReceipt | None:
        from web3.exceptions import TransactionNotFound

        try:
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            return None

        block_number = int(_field(receipt, "blockNumber"))
        receipt_block_hash = _hex(_field(receipt, "blockHash"))
        canonical_block = self.w3.eth.get_block(block_number)
        canonical_hash = _hex(_field(canonical_block, "hash"))
        if canonical_hash != receipt_block_hash:
            return None

        confirmations = max(0, int(self.w3.eth.block_number) - block_number + 1)
        transfers, withdrawals = self._decode_logs(list(_field(receipt, "logs", []) or []))
        return TxReceipt(
            tx_hash=tx_hash,
            status=int(_field(receipt, "status", 0)),
            gas_used=int(_field(receipt, "gasUsed", 0)),
            effective_gas_price=int(_field(receipt, "effectiveGasPrice", 0) or 0),
            block_number=block_number,
            block_hash=receipt_block_hash,
            confirmations=confirmations,
            transfers=transfers,
            withdrawals=withdrawals,
        )
