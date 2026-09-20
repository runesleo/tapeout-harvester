# Production Derivation Receipt

This RC is not a clean-room mock harvester. It was derived from the existing private TapeOut/BEM production harvester after a read-only source inspection.

## Source evidence

- Private production source SHA-256 observed through MCPX: `1d6c021eb8c1d8dbc7fea976d757959bd818e4e9af96525615fb6044fc12cbe4`
- Observed source size: 74,035 bytes / 1,996 lines.
- The private production file itself was not modified by this RC work.

## Preserved execution semantics

The RC keeps the reusable safety shape observed in production:

- `claimMany` across configured miner keys;
- signer deferred until live gates pass;
- simulation/gas checks before broadcast;
- exact ERC-20 approval hygiene rather than unlimited approval;
- fresh quote after claim and again before swap;
- receipt/state persistence;
- unknown-outcome recovery that reconciles a known tx hash instead of blind resend;
- local process/nonce conflict protection;
- post-transaction balance-delta checks.

## Intentionally removed or parameterized

The RC does not contain the author's:

- wallet address or miner inventory;
- encrypted wallet material, keychain identifiers or credentials;
- machine-specific filesystem paths;
- production scheduler labels/log paths;
- tier thresholds, cash/reinvestment split or portfolio policy;
- sibling-bot names or unrelated trading logic.

Those production-specific concerns were either removed or replaced with explicit user configuration. The open-source default remains claim-only + dry-run until the user opts into more.
