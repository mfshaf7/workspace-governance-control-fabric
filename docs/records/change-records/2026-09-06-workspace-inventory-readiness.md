# Workspace Active Inventory Readiness

## Summary

- Date: 2026-09-06
- Environment: source and isolated API/database tests
- ART: Epic #890, Feature #1070, child #1072
- Landing unit: `delivery-890-inventory-readiness`
- Owner: `workspace-governance-control-fabric`

## Boundary

Workspace Governance owns canonical intake and active-inventory YAML. WGCF
evaluates an exact promotion request against committed authority and stores the
resulting readiness artifact immutably. OOS owns durable workflow, source-change
preparation, review and merge waits, and merged readback. This change performs
no canonical mutation and activates no runtime.

## Source Changes

- Pin the exact #1071 promotion contract and v2 inventory schemas.
- Evaluate admitted status, exact source bindings, inventory absence, typed
  active records, compatibility aliases, approval references, and identity reuse.
- Expose authenticated issue and caller-scoped readback endpoints.
- Persist readiness and ledger evidence atomically through migration `0009`.
- Keep runtime activation disabled pending the later workflow and activation gates.

## Evidence

Focused tests use a real temporary Git authority and temporary SQL database.
They cover all three inventory kinds, ready, blocked, and stale projections,
exact replay, caller isolation, dirty-worktree exclusion, contract drift,
tampering, unavailable storage, malformed requests, and disabled activation.

## Next Boundary

OOS #1073 consumes this exact readiness artifact to coordinate the reviewed
Workspace Governance source change and merged canonical readback. Console #1074
projects the resulting registry truth; neither becomes authority.
