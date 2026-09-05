# Workspace Intake Readiness

## Summary

- Date: 2026-09-05
- Short title: Evaluate Workspace Intake classification readiness
- Environment: source and isolated API/database tests
- Severity: High

## Classification

- Type: non-mutating policy-readiness implementation
- ART: Epic #890, Feature #1061, child #1063
- Landing unit: delivery-890-intake-readiness
- Owner: workspace-governance-control-fabric

## Boundary

Workspace Governance owns classification, policy, and canonical records.
WGCF checks request/decision bindings against committed authority and persists
immutable readiness evidence. OOS owns workflow, exact review, merge wait, and
terminal readback. Platform owns provider identity and deployment; Security
owns trust acceptance. No canonical, provider, or ART mutation is implemented.

## Source Changes

- Pin Workspace Intake v2 authority from #1062.
- Evaluate all entrant kinds, classification metadata, owner routing,
  stale source/record bindings, add/update constraints, source immutability,
  applied idempotency keys, active-inventory overlap, and AI acceptance.
- Define a bounded transport envelope and immutable reference-only receipt.
- Authenticate evaluation/read through the existing WGCF caller boundary.
- Persist receipt and ledger event atomically in the existing database.
- Keep normal runtime activation disabled.

## Evidence

Focused tests exercise the isolated API with a real temporary Git authority
and a temporary SQL database. Positive/negative cases cover classification,
update/replay, ownership, stale bindings, operator and AI acceptance,
dispositions, tampering, auth, storage failure, and disabled activation.
Real-Git cases prove dirty files and unmerged branches are not authority and
changed contract revisions fail closed.

## Remaining Gates

#1064 must consume this exact interface and preserve operator acceptance,
review/merge waits, fresh source comparison, and merged-authority readback.
#1066 reviews trust acceptance; #1082 activates the approved composition.
This implementation is not live operating proof or Security acceptance.
