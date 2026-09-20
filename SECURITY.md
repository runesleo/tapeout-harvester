# Security

## Secret handling

- Never store seed phrases or raw private keys in this repository.
- Never store the keystore password in `.env`, `config.toml`, shell history or an AI conversation.
- Live signing uses a local encrypted JSON keystore and an allowlisted secure OS keyring backend.
- Plaintext/alternative keyring backends are rejected.
- `config.toml`, `wallet.json`, `state/` and `receipts/` are ignored by default.
- CLI errors do **not** echo arbitrary provider exception text. Unknown third-party details are reduced to `RUNTIME_DETAIL_REDACTED`; only short internal error codes are preserved.

## Execution safety model

- Dry-run is default and never loads the signer.
- Live mode requires the config flag **and** `--live`.
- Approval/swap construction is sequential: claim must finalize before approval; exact approval must finalize before swap.
- Claim/swap accounting uses transaction-specific **net** ERC-20 transfer attribution; unwrap uses the transaction-specific wrapped-native withdrawal event.
- Receipts must match the canonical block hash and satisfy `min_confirmations`.
- Accepted prerequisite receipts are revalidated before later economic actions, including immediately before swap/unwrap broadcast.
- Inflight state carries a hash of chain/wallet/miner/contracts/route and execution-policy identity. A changed identity blocks recovery.
- Expected tx hash, intent and gas commitment are durably persisted with file + directory `fsync` before broadcast.
- A signed transaction that is explicitly known not to have been broadcast is journaled and recovery routes through cleanup rather than rebroadcast.
- Unknown/missing receipts never cause automatic rebroadcast.
- Maximum gas is a cumulative worst-case cycle commitment, including bounded cleanup reserve.
- Allowance created by the current cycle is paired with a reserved cleanup budget. Known sell-path failure attempts cleanup before terminal block.
- Quote age is checked after prerequisite receipt validation and again at pre-broadcast boundaries. Swap deadline derives from the quote timestamp.
- `min_out` includes both target quote slippage and the configured hard floor versus pool spot.

## Concurrency model

The tool takes:

1. a stable private local lock derived from `chain_id + wallet`;
2. the configured state lock.

This coordinates this tool's local instances only. It **cannot** lock unrelated wallet apps or bots. Immediately before signing, the adapter re-checks `latest` vs `pending` nonce and requires the transaction nonce to still be current.

Use exactly **one authoritative `state_path` per chain + wallet**. A different state journal is not automatically discovered merely because it shares the wallet lock.

## Approval cleanup boundary

The engine automatically owns cleanup for allowance it creates in the current cycle. Sell mode can also reset a pre-existing mismatched router allowance before setting the exact amount. Therefore the same token/router allowance should not be concurrently owned by unrelated automation.

Cleanup gas is intentionally bounded. The current reserve uses a fee ceiling derived from the observed fee; a sufficiently large later fee spike can leave cleanup pending or require manual revocation. In that condition the engine stays blocked and does not continue trading.

## State durability and local filesystem boundary

State is written to a temporary file, `fsync`ed, atomically replaced, and the parent directory is `fsync`ed before broadcast continues. Old state schemas are rejected rather than silently reinterpreted.

Use state/runtime directories owned by the operator and not writable by untrusted local users. The default design assumes a trusted single-user runtime. Deliberately placing the state file in a shared/untrusted writable directory can expose filesystem races (including hardlink attacks on predictable temporary names).

## Reporting

Before a public repository exists, report issues privately to the repository owner. Never attach wallet exports, private config or unredacted transaction-operational logs.
