# Repository Custody Provider ID Sync

## Summary

- Date: 2026-08-29
- Short title: Synchronize GitHub REST repository identity
- Environment: WGCF source only
- Severity: Medium

## Classification

- Type: upstream contract synchronization
- User-facing impact: Invalid GitHub GraphQL node IDs now fail before a
  readiness decision is issued. Runtime activation remains disabled.

## Ownership

- Owning repo or layer: `workspace-governance-control-fabric`
- Related repos: `workspace-governance`, `operator-orchestration-service`
- Related architecture: Workspace Delivery ART Epic `#888`, children `#1042`
  and `#1043`

## Boundary

- Workspace Governance remains the repository identity contract authority.
- WGCF retains an exact digest-pinned consumer copy and evaluates only requests
  that satisfy that authority.
- OOS remains the provider-adapter and custody-receipt authority.

## Source Changes

- Updated the pinned Workspace Governance source revision.
- Synchronized the authority and all repository-custody artifact schemas.
- Enforced GitHub's positive decimal REST repository `id` across requests,
  decisions, readbacks, and receipts.
- Added negative readiness coverage for a GraphQL `node_id` supplied in the
  REST repository-ID field.

## Evidence

- Repository-custody readiness tests pass against the synchronized bundle.
- The bundle loader verifies every copied file against its manifest digest.
- The negative provider-ID case fails before a readiness decision is stored.

## Residual Work

- OOS child `#1042` must consume the same source revision and validate the
  provider response's numeric `id` before Security acceptance is issued.
