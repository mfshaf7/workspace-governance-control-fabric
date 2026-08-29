# Repository Provisioning Readiness

## Summary

- Date: 2026-08-29
- Short title: Evaluate repository provisioning controls
- Environment: WGCF source and sandbox runtime only
- Severity: High

## Classification

- Type: policy-readiness implementation
- User-facing impact: OOS can obtain an immutable `create-provider` decision
  for an exact approved GitHub organization target and baseline settings.
  Normal runtime activation remains disabled.

## Ownership

- Owning repo or layer: `workspace-governance-control-fabric`
- Related repos: `workspace-governance`, `operator-orchestration-service`,
  `security-architecture`, `platform-engineering`
- Related architecture: Workspace Delivery ART Epic `#888`, child `#1055`

## Boundary

- Workspace Governance owns provisioning policy and artifact schemas.
- WGCF validates and persists readiness decisions only.
- OOS owns the later provider command, reconciliation, readback, and terminal
  receipt.
- Security and Platform own GitHub App write acceptance and runtime delivery.

## Source Changes

- Synchronized the exact Workspace Governance provisioning authority and
  schemas from merged child `#1054`.
- Added action-specific readiness for `provision-new` while preserving
  `link-existing` behavior.
- Bound allowed provisioning decisions to the exact organization, repository
  name, visibility, initialization, feature toggles, and merge policy.
- Preserved one request identity, one immutable decision, exact replay, and
  fail-closed handling for changed payloads and tampered ledger records.

## Evidence

- Positive sandbox cases issue, replay, and read one `create-provider`
  decision without provider mutation.
- Negative cases reject personal scope, incomplete settings, unsupported
  providers, stale policy, secret-bearing references, and conflicting replay.
- The normal runtime builder remains blocked by the upstream activation flag.

## Residual Work

- OOS child `#1046` must consume the exact decision and implement idempotent
  provider execution and readback.
- Security child `#1047` and Platform child `#1048` must approve and deliver
  the organization-scoped GitHub App authority before runtime activation.
