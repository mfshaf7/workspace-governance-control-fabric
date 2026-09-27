# Lifecycle Transition Readiness

## Summary

- Date: 2026-09-28
- Short title: Evaluate cross-domain lifecycle transition readiness
- Environment: source and isolated API/database tests
- Severity: High

## Classification

- Type: non-mutating readiness implementation
- ART: Epic #900, Feature #931, child #1194
- Landing unit: `delivery-900-wgcf-lifecycle-evidence`
- Owner: `workspace-governance-control-fabric`

## Boundary

Workspace Governance owns lifecycle policy, OOS owns workflow and transition
state, and source and target domains own their records. WGCF validates one exact
OOS projection and persists caller-scoped readiness, ledger, and escalation
evidence. It does not apply a transition or mutate an owner record.

## Source Changes

- Pin the Workspace Governance lifecycle authority and OOS projection contract.
- Add bounded evaluation and immutable readiness schemas.
- Evaluate route binding, freshness, ordered history, state-specific evidence,
  and safe evidence references for the three admitted routes.
- Persist readiness and escalation evidence through migration
  `0014_lifecycle_readiness`.
- Expose authenticated issue and readback routes for OOS and the WGCF
  reconciler.

## Evidence

Focused tests cover every admitted route, terminal-state proof, returned and
blocked states, stale source, unsafe references, caller-scoped readback,
idempotent reuse, replay conflict, contract tamper, request bounds, and runtime
activation denial.

## Remaining Gates

Console projection, Security acceptance, and Platform activation remain in
their existing Epic #900 children. This source change does not activate the
runtime.
