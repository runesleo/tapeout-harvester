# TapeOut Harvester

A fail-closed, locally signed reward harvester for TapeOut/BEM. It can **claim rewards only** or, when explicitly configured, **claim and swap a chosen fraction** through an allowlisted V3 route.

> Local release candidate: `0.1.0rc3` (unreleased). Dry-run is the default. Live execution requires **both** `runtime.live_enabled = true` and the CLI `--live` flag.

[中文说明](README.zh.md) · [AI setup guide](AI_SETUP.md) · [Security](SECURITY.md)

## What you get

- Reads current claimable rewards for configured miner keys.
- Claims with `claimMany` only after threshold, gas and safety gates pass.
- Optional swap of **only the reward amount attributed to this cycle's claim transaction**.
- Transaction-specific ERC-20 `Transfer` / wrapped-native `Withdrawal` receipt attribution instead of wallet-balance-delta guessing.
- Configurable minimum confirmations plus canonical-block receipt checks before advancing state.
- Router allowlist, quote freshness, total spot-deviation hard limit and swap deadline bound to the quote timestamp.
- Exact ERC-20 approval hygiene. Allowance created by this cycle is reserved for cleanup and revoked on bounded sell-path failures.
- Expected tx hash and intent are durably persisted with file + parent-directory `fsync` before broadcast.
- No blind resend on unknown outcome.
- Inflight state is bound to chain, wallet, miner set, contracts and execution policy; changing them mid-cycle blocks recovery.
- Cumulative worst-case gas commitment across the cycle, including reserved cleanup gas.
- A stable chain+wallet local lock plus a nonce re-check immediately before every signature.
- Encrypted JSON keystore + approved OS keyring backend; no plaintext private-key config.
- RPC candidates must pass a representative TapeOut `pending` read before selection; `watch` reselects every cycle.
- Durable watch heartbeat plus a read-only `doctor` command for stale-daemon/RPC/state diagnosis.

## How it works

```text
pending rewards
    ↓
minimum-claim + claim gas gates
    ↓
claim simulation
    ↓
[dry-run stops here; sell mode may fetch a quote preview only]
    ↓ live double opt-in
local signer → persist tx intent/hash → claim
                                  ↓ confirmations + canonical receipt
                          attribute reward Transfer from this tx
                                  ↓
             claim-only ──────────┴──────→ receipt
                                  ↓ optional sell
                         fresh quote + route gates
                                  ↓
             reset old mismatch → exact approve
                                  ↓ confirmed allowance
                          fresh quote + swap simulation
                                  ↓
                    persist intent/hash → swap
                                  ↓
             attributed destination Transfer
                                  ↓
   cleanup cycle-created allowance if needed → optional unwrap
                                  ↓
                       attributed Withdrawal → receipt
```

The public package is intentionally separated from the author's private wallet inventory, machine paths, thresholds, capital-allocation policy, logs and production schedulers.

## Requirements

- Python 3.11+
- BSC RPC with `eth_call`, gas estimation, transaction submission, receipts and block lookup
- Current official TapeOut/BEM contract addresses and your miner keys
- Native gas token in the execution wallet
- Optional sell mode: verified V3 router, quoter, pool, fee tier and destination token
- Live mode: encrypted JSON keystore + supported secure OS keyring backend

Verified final release environment for RC2: **Python 3.11.4, web3.py 7.16.0, eth-account 0.14.0, keyring 25.7.0**. The package accepts `web3>=7.12,<8`; v8 is intentionally not assumed compatible.

### Privacy

Never paste seed phrases, raw private keys or keystore passwords into an AI chat. If AI helps install the repo, let it edit only non-secret config. The tool rejects plaintext/alternative keyring backends that are not in its secure OS-backend allowlist.

## Setup

```bash
git clone <REPO_URL>
cd tapeout-harvester
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[keyring]'
cp config.example.toml config.toml
```

Fill `config.toml` with values independently verified from current official sources. The example addresses and economic values are deliberately non-production examples.

```bash
tapeout-harvester --config config.toml check
```

## Dry-run first

```bash
tapeout-harvester --config config.toml run
```

Dry-run never loads the signer or broadcasts. It:

1. reads pending rewards;
2. builds/simulates the **claim**;
3. checks claim gas/reserve gates;
4. if selling is configured, fetches a **sell quote preview**.

It deliberately does **not** pretend it can fully simulate approve/swap before the claim exists. Live approve/swap construction happens only after the claim receipt has enough confirmations and the claimed amount is attributed from that transaction.

Claim-only mode:

```toml
[harvest]
sell_fraction = "0"
```

## Live mode

1. Keep `runtime.live_enabled = false` during setup.
2. Put an encrypted JSON keystore at the configured local path.
3. Store its password in the approved OS keyring:

```bash
tapeout-harvester --config config.toml signer store-password
```

4. Run dry-run again.
5. Set `runtime.live_enabled = true`.
6. Explicitly invoke:

```bash
tapeout-harvester --config config.toml run --live
```

Both switches are required.

If a receipt is missing or non-canonical, the engine keeps the known tx hash and does not resend. It waits for the configured confirmation policy. An inflight cycle also refuses to continue under a changed wallet/chain/miner/route/execution policy.

## Scheduled operation

```bash
tapeout-harvester --config /absolute/path/config.toml watch
```

Without `--live`, watch mode remains dry-run. Each watch cycle creates a fresh adapter and re-checks the configured RPC list using an actual TapeOut `pending` call before choosing a provider.

Every completed watch cycle writes a redacted heartbeat next to the state journal. To inspect RPC, state and watch freshness without loading the signer or broadcasting anything:

```bash
tapeout-harvester --config /absolute/path/config.toml doctor
```

A stale, error, blocked-cycle heartbeat, or persisted `BLOCKED_SAFE` journal returns a degraded status. Heartbeat writes are serialized separately so concurrent watchers cannot corrupt the shared heartbeat temp file. No heartbeat yet is reported as `NOT_OBSERVED`, which is not treated as a failure when the journal itself is healthy.

The stable chain+wallet lock coordinates **this tool's local instances across config directories**. It cannot lock unrelated wallet apps, bots or processes. The signer therefore re-checks the wallet nonce immediately before every signature and fails closed if another writer has moved it. Use one authoritative `state_path` per chain + wallet; the wallet lock does not automatically merge or discover separate journals.

## Gas and approval model

- `max_gas_native_per_cycle` is enforced against cumulative **maximum signed gas commitment**, not just each transaction separately.
- When this cycle creates an exact sell allowance, it reserves bounded gas for a cleanup revoke before approving.
- If swap construction/broadcasted execution fails in a known state, the engine attempts the reserved revoke and then terminates blocked.
- A pre-existing exact allowance not created by this cycle is not silently claimed as tool-owned state and may remain after a failed swap.
- If cleanup itself is unknown or fails, the engine stays blocked; it does not keep trading.

## Verified in local RC3 candidate

The local dependency-backed suite contains **100 tests**:

```text
Python 3.11.4
web3 7.16.0
eth-account 0.14.0
keyring optional surface tested with keyring installed
100/100 PASS, 0 skipped
```

The suite includes dependency-backed adapter checks plus state-machine, restart/unknown-outcome, config, signer, durability, filesystem-lock and security-preflight coverage. The RC2 engine baseline previously completed an independent Codex blocker-focused review with **Blockers: None**. Nine GPT-6 Astra/xhigh RC3 review passes found fifteen P2 diagnosis/health issues in total. The ninth pass bound heartbeat health to the watcher's startup configuration fingerprint so doctor cannot treat a stale worker running old policy as healthy. All fifteen findings are remediated with focused regressions and the full 100-test suite passing. The final GPT-6 Astra/xhigh read-only rerun found no actionable regressions.

Run:

```bash
python -m unittest discover -s tests -v
```

See `VERIFICATION.json` and `REVIEW-codex-pass.md` for exact gates and independent review status.

## Known limitations / operational boundaries (`0.1.0rc3`)

- Cleanup gas is bounded. A sufficiently large later fee spike can strand a revoke and require manual revocation; the engine remains blocked rather than continuing to trade.
- Use **one authoritative state journal per chain + wallet**. A second `state_path` is not automatically reconciled with an unresolved first journal.
- Sell mode may reset a pre-existing mismatched router allowance, and cycle cleanup may revoke the current allowance. Do not share the same token/router allowance with unrelated automation unless that behavior is intended.
- Use state/runtime directories owned by the operator and not writable by untrusted local users; deliberately unsafe shared directories can expose filesystem races around temporary state files.
- POSIX `fcntl` locking; native Windows process locking is not included.
- V3 single-pool/single-hop swap only; no aggregator or route discovery.
- Contract addresses are intentionally not embedded.
- The local wallet lock cannot coordinate unrelated wallet software; nonce re-check is the fail-closed fallback.
- Transaction attribution relies on standard ERC-20 `Transfer` and wrapped-native `Withdrawal(address,uint256)` event semantics.
- RC3 is currently an **unreleased local candidate**. It has unit/synthetic + real-library verification; no rc3 public release or fresh live-funds execution has occurred.
- web3.py v8 remains unverified.
- Licensed under the MIT License. Public GitHub publication is owner-approved and verified separately from code safety.

## Roadmap

- Fork/read-only-chain adapter fixtures using current official contracts.
- Additional local signer surfaces without weakening secret isolation.
- Multiple verified route adapters without becoming a generic trading bot.

## About

Built from a real TapeOut/BEM harvesting workflow and stripped of private wallet inventory, personal strategy parameters and internal infrastructure.

## License

MIT — Copyright (c) 2026 `runesleo`. See `LICENSE`.
