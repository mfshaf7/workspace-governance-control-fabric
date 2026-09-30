# Delivery ART Architecture v4 Custody

## Summary

- Date: 2026-09-30
- Environment: source and isolated registry/readiness tests
- Tracking: `workspace-governance/reviews/improvement-candidates/2026-09-30-delivery-art-historical-artifact-compatibility-gap.yaml`
- Landing unit: `maintenance-delivery-art-architecture-v4-custody`
- Owner: `workspace-governance-control-fabric`

## Boundary

Workspace Governance remains the schema and lifecycle authority. OOS remains
the artifact producer, current-pointer writer, and active work-session
inventory owner. WGCF owns immutable artifact custody and readiness decisions.
It does not infer capability declarations, mutate ART, or decide whether an
active session may be moved to another architecture packet.

## Source Changes

- Pin the Workspace Governance Architecture Packet v4 schema from commit
  `120f520baa3eba9e1b36c1caef6300dcef8b6a77`.
- Require schema v4 for new architecture registration and fresh
  `architecture-ready` decisions.
- Preserve immutable readback and dependency validation for schema v1-v3
  packets already present in custody.
- Validate both historical prose boundaries and capability-based boundaries
  without translating one form into the other.
- Apply v3 execution-plan, handoff, and human-gate semantics to v4.
- Preserve exact same-subject supersession for historical-to-v4 replacement.

## Evidence

Focused tests prove current v4 custody and readiness, rejection of new v1-v3
registration, rejection of fresh historical architecture readiness, immutable
historical readback after runtime upgrade, continued readiness for work already
bound to a historical prose packet, exact historical-to-v4 supersession, and
fail-closed v4 topology and execution-plan validation.

## Non-Goals

- No active-session inventory or current-pointer cutover logic in WGCF.
- No historical packet rewrite or inferred capability mapping.
- No new API, database, service, or operator workflow.
