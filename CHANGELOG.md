# Changelog

All notable changes to this project will be documented here. The format follows Keep a Changelog.

## [0.1.0rc3] - Unreleased

### Reliability
- RPC selection now requires a representative TapeOut `pending` contract read, not only `chainId` / latest block. Nodes with a broken current state trie are skipped before use.
- `watch` rebuilds the adapter every cycle so a provider chosen hours earlier is not pinned forever.
- `watch` writes a durable, redacted heartbeat next to the state journal after each cycle and on fatal cycle errors; blocked-cycle results are explicitly degraded.
- Heartbeat writes use a dedicated blocking lock so concurrent watchers cannot race on the shared temporary file.
- Added a read-only `doctor` command that checks end-to-end RPC reads, terminal/inflight state, persisted `BLOCKED_SAFE` failures, inflight config-identity drift, unresolved receipt timeout/query failures, confirmed reverts, confirmed receipt attribution/allowance invariants, signed-not-broadcast state, blocked results, and heartbeat freshness after RPC probing without loading the signer.

### Verification
- Added focused tests for representative RPC rejection, heartbeat redaction/serialization, blocked-result degradation, COMPLETE terminal handling, no-heartbeat behavior, and post-RPC stale-heartbeat detection.
- Nine GPT-6 Astra/xhigh RC3 review passes found fifteen P2 diagnosis/reliability issues in total; all fifteen were remediated. The final independent read-only rerun found no actionable regressions.
- Local candidate suite after remediation: **100/100 PASS, 0 skipped** in a fresh dependency-backed environment before final clean review.
- Fresh `.[dev,keyring]` install is self-contained for verification; the `dev` extra now includes pytest.
- No author production price bands, wallet inventory, reinvest rules, or capital-allocation policy are included.

## [0.1.0rc2] - 2026-09-20

### Security
- Replaced wallet-balance-delta attribution with transaction-specific net `Transfer` / `Withdrawal` receipt attribution.
- Bound inflight recovery to chain, wallet, miner set, contracts, route and execution policy.
- Made claim → approval → swap construction strictly sequential.
- Added canonical-block receipt validation, configurable confirmations, and prerequisite receipt revalidation before later economic actions.
- Added cumulative worst-case gas commitment plus bounded cleanup gas for cycle-created allowance.
- Added restart-safe `SIGNED_NOT_BROADCAST` recovery and bounded revoke cleanup on known sell-path failures.
- Added file and directory `fsync` before broadcast can proceed.
- Added a stable private chain+wallet lock, no-follow/link/owner lock checks, and nonce re-check immediately before signing.
- Added strict TOML boolean/integer parsing, runtime path-collision rejection, fail-closed provider error rendering, and secure-keyring backend enforcement.
- Tightened quote timing checks through the pre-broadcast boundary and added a hard spot-deviation floor.
- Security preflight findings no longer echo matched secret values.

### Verification
- Expanded the suite from 28 to **65 tests**.
- Verified **65/65 PASS, 0 skipped** on Python 3.11.4 / web3.py 7.16.0 / eth-account 0.14.0 / keyring 25.7.0.
- Final independent blocker-focused Codex review: **0 blockers**.
- Additional independent fault injection covered 72 before/after-save crash boundaries without duplicate claim/swap/unwrap or unrelated wrapped-token consumption.
- RC2 has not been live-traded from the public package.

## [0.1.0rc1] - 2026-09-20

### Added
- Production-derived TapeOut `claimMany` reward flow with claim-only default.
- Optional V3 single-hop swap with explicit route allowlist and configurable sell fraction.
- Quote freshness/deviation, gas cap and native gas-reserve gates.
- Exact approval/reset/revoke lifecycle.
- Pre-broadcast tx-hash persistence and fail-closed unknown-outcome reconciliation.
- Atomic state, single-process lock and JSON receipts.
- Encrypted keystore + OS keyring local signer option.
- English/Chinese documentation and AI setup contract.

### Security
- No author production wallet, miner inventory, private host paths, credentials, logs or capital-allocation policy are included.
- Live execution is double-gated and signer loading is deferred until non-signing gates pass.
