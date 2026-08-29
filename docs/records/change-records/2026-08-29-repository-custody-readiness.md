# Repository Custody Readiness

## Summary

- Date: 2026-08-29
- Short title: Immutable existing-repository custody readiness decisions
- Environment: WGCF source and injected sandbox runtime
- Severity: Medium

## Classification

- Type: control-fabric readiness implementation
- User-facing impact: Prepares the policy decision consumed by the later OOS
  existing-repository linkage workflow without activating that workflow.

## Ownership

- Owning repo or layer: `workspace-governance-control-fabric`
- Related repos: `workspace-governance`, `operator-orchestration-service`,
  `platform-engineering`, `security-architecture`,
  `governance-operations-console`
- Related architecture: Workspace Delivery ART Epic `#888`, child `#1041`

## Boundary

- Workspace Governance owns the custody contract and schemas.
- WGCF evaluates request readiness and stores immutable decisions.
- OOS owns workflow state, provider invocation, custody mutation, and terminal
  receipts.
- Platform owns provider identity and credential delivery.
- Security owns trust acceptance.

## Source Changes

- The exact `#1040` authority bundle is pinned by source commit and byte digest.
- Canonical request integrity, policy freshness, first-capability scope,
  authenticated caller scope, replay, conflict, and secret-reference checks
  fail closed.
- Decisions are immutable and stored with compact ledger events.
- Runtime activation requires both an upstream activation decision and an
  explicit dev-integration environment gate.

## Evidence

- Positive and negative sandbox-runtime conformance tests cover the `#1041`
  architecture cases.
- The finalized ART `#1041` Review Packet records the merged commit and exact
  CI-equivalent validation commands.

## Residual Work

- `#1042` implements OOS workflow and provider readback.
- `#1043` and `#1044` own Security review and Platform identity activation.
- `#1045` connects the Console projection and completes runtime activation.
