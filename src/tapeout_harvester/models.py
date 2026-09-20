from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Quote:
    amount_in_raw: int
    amount_out_raw: int
    min_out_raw: int
    quoted_at: float
    execution_price: Decimal
    spot_price: Decimal
    deviation_bps: Decimal


@dataclass(frozen=True)
class TokenTransfer:
    token: str
    sender: str
    recipient: str
    amount_raw: int


@dataclass(frozen=True)
class NativeWithdrawal:
    token: str
    owner: str
    amount_raw: int


@dataclass(frozen=True)
class TxReceipt:
    tx_hash: str
    status: int
    gas_used: int = 0
    effective_gas_price: int = 0
    block_number: int | None = None
    block_hash: str | None = None
    confirmations: int = 0
    transfers: tuple[TokenTransfer, ...] = ()
    withdrawals: tuple[NativeWithdrawal, ...] = ()

    def received_raw(self, token: str, recipient: str) -> int:
        token = token.lower()
        recipient = recipient.lower()
        return sum(
            int(item.amount_raw)
            for item in self.transfers
            if item.token.lower() == token and item.recipient.lower() == recipient
        )

    def net_received_raw(self, token: str, account: str) -> int:
        token = token.lower()
        account = account.lower()
        incoming = sum(
            int(item.amount_raw)
            for item in self.transfers
            if item.token.lower() == token and item.recipient.lower() == account
        )
        outgoing = sum(
            int(item.amount_raw)
            for item in self.transfers
            if item.token.lower() == token and item.sender.lower() == account
        )
        return incoming - outgoing

    def withdrawn_raw(self, token: str, owner: str) -> int:
        token = token.lower()
        owner = owner.lower()
        return sum(
            int(item.amount_raw)
            for item in self.withdrawals
            if item.token.lower() == token and item.owner.lower() == owner
        )


@dataclass(frozen=True)
class SignedTx:
    tx_hash: str
    raw_transaction: bytes


@dataclass
class RunResult:
    status: str
    reason: str | None = None
    live: bool = False
    signer_loaded: bool = False
    pending_raw: int | None = None
    claimed_raw: int | None = None
    sell_raw: int | None = None
    received_raw: int | None = None
    stage: str | None = None
    tx_hashes: dict[str, str] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)

        def convert(value: Any) -> Any:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [convert(v) for v in value]
            return value

        return convert(out)
