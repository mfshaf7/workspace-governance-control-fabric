# Repository Custody Terminal Receipt Sync

## Summary

- Date: 2026-08-29
- Short title: Synchronize truthful terminal receipt conditions
- Environment: WGCF source only
- Severity: Low

## Classification

- Type: upstream contract synchronization
- User-facing impact: None; runtime activation remains disabled.

## Ownership

- Owning repo or layer: `workspace-governance-control-fabric`
- Related repos: `workspace-governance`, `operator-orchestration-service`
- Related architecture: Workspace Delivery ART Epic `#888`, child `#1042`

## Boundary

- Workspace Governance remains the contract authority.
- WGCF retains an exact digest-pinned consumer copy.
- OOS remains the terminal custody receipt authority.

## Source Changes

- Updated the pinned Workspace Governance source revision.
- Synchronized the authority, contract schema, and receipt schema.
- Denied and failed outcomes may now retain null provider readback; successful
  actions that require provider truth still require digest-bound readback.

## Evidence

- Repository-custody readiness tests pass against the synchronized bundle.
- The bundle loader verifies every copied file against its manifest digest.

## Residual Work

- OOS child `#1042` must consume the same source revision and issue canonical
  terminal receipts.
