# Repository Lifecycle Readiness

## Summary

- Date: 2026-08-30
- Short title: Evaluate repository lifecycle transitions
- Environment: WGCF source and sandbox runtime only
- Severity: High

## Classification

- Type: policy-readiness implementation
- User-facing impact: OOS can obtain an immutable readiness decision for one
  exact transfer, archive, unarchive, retire, or restore request. Normal runtime
  activation remains disabled.

## Ownership

- Owning repo or layer: `workspace-governance-control-fabric`
- Related repos: `workspace-governance`, `operator-orchestration-service`,
  `security-architecture`, `platform-engineering`
- Related architecture: Workspace Delivery ART Epic `#888`, Feature `#915`,
  child `#1059`

## Boundary

- Workspace Governance owns lifecycle vocabulary, policy, and artifact schemas.
- WGCF validates and persists immutable readiness decisions only.
- OOS owns command lifecycle, state mutation, readback, terminal receipts, and
  history.
- Platform owns provider credentials; Security owns trust acceptance.

## Source Changes

- Synchronized the exact Workspace Governance lifecycle authority and schemas.
- Added a separate lifecycle evaluator and immutable decision ledger.
- Preserved independent custody, provider, and workspace-record state axes.
- Added exact impact, confirmation, provider-version, reversal, and next-action
  evaluation without downstream mutation.
- Added authenticated issue/read API routes that remain activation-gated.

## Evidence

- Positive sandbox cases cover all five lifecycle actions, deterministic replay,
  immutable readback, exact gates, and bounded next actions.
- Negative cases cover stale policy, secret-bearing references, conflicting
  replay, unsupported actions, invalid impact counts, missing provider versions,
  tampered ledger state, and deferred blockers.
- The normal runtime builder remains blocked by the upstream activation flag.

## Residual Work

- OOS child `#1051` must consume the exact decision and implement guarded state
  mutation, readback, reversal, and terminal receipts.
- Console child `#1053` must project current truth, impact, confirmation,
  lifecycle actions, and immutable history without fixture fallback in live
  mode.
