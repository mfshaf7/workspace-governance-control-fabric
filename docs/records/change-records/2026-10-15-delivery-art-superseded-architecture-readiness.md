---
security_evidence:
  review_areas:
    - delivery
    - runtime
  reviewed_artifacts:
    - packages/control_fabric_core/src/control_fabric_core/delivery_art_readiness.py
    - tests/test_delivery_art_readiness.py
    - docs/operations/operator-surface.md
  findings: []
  risks: []
  workstreams:
    - WS-007
  notes: "The repair preserves fail-closed semantic validation for every active readiness dependency and narrows only immutable superseded Architecture Packet lineage. Independent Security review of the exact source head remains required before merge."
---

# Delivery ART Superseded Architecture Readiness

## Summary

- Date: 2026-10-15
- Environment: source and isolated registry/readiness tests
- Tracking: `workspace-governance/reviews/improvement-candidates/2026-10-14-delivery-art-human-gate-topology-regression.yaml`
- Landing unit: `maintenance-1275-wgcf-historical-supersession-readiness`
- Owner: `workspace-governance-control-fabric`

## Root Cause

The combined-topology validator correctly rejected new cyclic Architecture
Packets, but WGCF also reapplied that newer semantic rule while traversing the
`custody.supersedes` ancestor of a corrected active packet. The immutable
predecessor had been admitted before the validator existed, so the corrected
packet could not progress through later readiness even though its own topology
was valid.

## Source Changes

- Fully validate the active Architecture Packet used by readiness.
- Resolve an Architecture Packet predecessor as exact, content-addressed,
  integrity-checked historical lineage without reactivating its old semantics.
- Keep full semantic validation for Review Packet predecessors because they are
  active finalization evidence, not architecture audit lineage.
- Add a regression proving that the cyclic predecessor still fails as an active
  readiness subject while a corrected successor can support later work-start
  readiness.

## Boundary

This repair adds no authority, identity, storage class, service, or runtime
capability. New architecture registration and active readiness still fail
closed on the combined topology rule. OOS remains current-pointer and active
session authority; WGCF preserves immutable custody and readiness evaluation.

## Validation

- focused Delivery ART readiness tests
- full repository test suite
- project validation and exact-base source diff validation

## Rollback

Revert this Landing Unit. The prior behavior will again reject a corrected
packet whenever its audit lineage includes a predecessor admitted before a
later semantic validator.
