# Independent Codex Review — 0.1.0rc2

Date: 2026-09-20

## Result

**Final release-blocker verdict: 0 blockers.**

This is not a claim that the software is risk-free. It means the final independent read-only review did not reproduce a remaining release-blocking correctness or security flaw after the remediation passes below.

## Review progression

- Pass 1: 8 blockers reproduced.
- Pass 2: 4 blockers reproduced.
- Pass 3: 3 blockers reproduced.
- Final blocker-focused pass: **Blockers: None**.

The reviews were run independently from the implementation reasoning, with read-only local fault injection. No credentials were accessed and no live blockchain transaction was broadcast.

## Fresh final evidence

- Full release suite: **65/65 PASS, 0 skipped**.
- Python compile check: PASS.
- Project security preflight: PASS.
- Final independent review additionally exercised 57 selected checks and 72 before/after-save crash boundaries.
- Those crash-boundary probes found no duplicate claim/swap/unwrap and no consumption of unrelated wrapped-token inventory.
- Offline real-library checks covered Web3 / eth-account transaction construction and signing without network or live funds.
- Public RC has **not** been live-traded with fresh funds.

## Prior blockers verified closed

The final review explicitly rechecked:

1. a crash after `SIGNED_NOT_BROADCAST` can resume into allowance cleanup without rebroadcasting the signed swap;
2. slow prerequisite receipt reads cannot let an expired quote proceed to swap broadcast;
3. a changed/noncanonical swap receipt blocks unwrap before signing and again before broadcast.

Earlier blocker classes also included external-deposit misattribution, inflight identity drift, allowance cleanup, premature approve/swap simulation, TOML coercion, journal durability, cumulative gas accounting, and provider-error secret leakage.

## Remaining warnings / operational boundaries

These are not release blockers, but operators should understand them:

1. **Cleanup gas is bounded, not unlimited.** The current cleanup reserve uses a bounded fee ceiling derived from the earlier observation. A larger fee spike can leave a revoke pending or require manual revocation. The engine remains blocked rather than continuing to trade.
2. **One authoritative journal per chain + wallet.** The wallet lock serializes this tool, but a separate configuration can still point at another state journal. Do not run one wallet through multiple independent `state_path` values while any cycle is unresolved.
3. **Router allowance ownership must be exclusive.** Sell mode may reset a pre-existing mismatched allowance, and cycle cleanup can revoke the current router allowance. Do not share the same token/router allowance with unrelated automation unless that behavior is intended.
4. **Runtime directories must be private/trusted.** The default local layout is intended for a single user. A deliberately unsafe directory writable by an untrusted local user can still create filesystem races around state temporary files.

## Release gates still outside this review

- Final LICENSE / copyright-holder activation: pending repository-owner confirmation.
- Public GitHub repository creation or push: not performed.
- Any public announcement: not performed.
