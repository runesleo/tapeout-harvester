# AI Setup Contract

This repo is designed so a user can hand it to a coding assistant without handing the assistant wallet secrets.

## Allowed automation

An assistant may:

1. create a virtual environment and install dependencies;
2. copy `config.example.toml` to `config.toml`;
3. ask the user to independently verify official **public** contract addresses and miner keys;
4. validate config and run unit tests;
5. run `check` and dry-run;
6. create a local launchd/systemd unit pointing at this repo;
7. inspect sanitized local state/receipts;
8. explain a fail-closed blocker and propose a non-broadcasting fix.

## Hard boundaries

An assistant must not:

- ask for or store a seed phrase, raw private key or keystore password;
- invent or guess contract/miner/router addresses;
- bypass router allowlists, confirmation policy, slippage/gas gates, identity binding or the two live-mode switches;
- treat a missing receipt as failed and resend the same economic action;
- disable the stable wallet lock or nonce re-check to “fix” concurrency;
- replace exact approval with unlimited approval;
- turn a blocked cleanup into “just continue anyway”;
- expose `config.toml`, `wallet.json`, state or receipts publicly without redaction.

## Safe setup sequence

```text
install
  → tests
  → fill public config
  → check
  → dry-run (claim simulation + optional sell quote preview)
  → inspect
  → user stores keystore password directly in approved OS keyring
  → dry-run again
  → user explicitly enables live
```

Live sell simulation is deliberately sequential: approve/swap are constructed only after prior on-chain stages have confirmed.

If a transaction outcome is unknown, stop. Reconcile the persisted tx hash and confirmation state before any new transaction is constructed.
