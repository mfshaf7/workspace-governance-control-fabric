---
security_evidence:
  review_areas:
    - delivery
    - runtime
  reviewed_artifacts:
    - contracts/delivery-art/delivery-art-architecture-packet.schema.json
    - contracts/delivery-art/fixtures/architecture-packet-v5-parity-vectors.valid.json
    - packages/control_fabric_core/src/control_fabric_core/delivery_art_contracts.py
    - packages/control_fabric_core/src/control_fabric_core/delivery_art_readiness.py
    - packages/control_fabric_core/src/control_fabric_core/artifact_registry.py
    - docs/operations/operator-surface.md
  findings: []
  risks: []
  workstreams:
    - WS-007
  notes: "The change adds dormant fail-closed v5 validation and readiness selection without activating v5 custody, architecture readiness, or runtime use. A separate Security delta review remains an activation prerequisite."
---

# Delivery ART Architecture V5 Readiness Support

## Summary

WGCF now understands staged Architecture Packet v5 evidence ownership and
exact readiness phases. V4 remains the only current custody and architecture
readiness version until the independent Security, activation, deployment, and
session-inventory gates complete.

## Classification

- area: Delivery ART contract validation, artifact custody, and readiness
- type: corrective evidence-integrity maintenance
- runtime impact: dormant v5 validation and readiness selection; no v5
  persistence or architecture-readiness activation

## Ownership

- owner repo: `workspace-governance-control-fabric`
- related ART slice: Epic #1203 recovery maintenance after work item #1228
- related improvement candidate:
  `workspace-governance/reviews/improvement-candidates/2026-10-03-post-merge-review-packet-architecture-recovery-regression.yaml`
- related products or components: Delivery ART contract validation, immutable
  custody, and readiness receipts

## Root Cause

- immediate failure: readiness selected conformance evidence by outcome-scope
  overlap and always evaluated merge-ready cases
- actual root cause: architecture v1-v4 did not separate outcome applicability
  from the Landing Unit accountable for producing evidence
- why it escaped earlier controls: OOS and WGCF lacked a shared parity vector
  proving exact owner-and-phase selection

## Source Changes

- pin the Workspace Governance v5 schema and canonical parity vector while v4
  remains current
- validate v5 evidence-owner existence and causal closure
- select v5 readiness cases by one exact Landing Unit and exact phase
- preserve v1-v4 overlap-based merge-case behavior unchanged
- reject v5 new custody and fresh architecture readiness until activation
- document the staged posture in the primary operator surface

## Artifact And Deployment Evidence

- source-only change, or build/deployment evidence: source-only dormant
  consumer support pending review and merge
- image tag or digest: None
- runtime revision: None; activation is a separate governed Landing Unit

## Live Verification

- local validation: focused Delivery ART suites, full Python suite, project
  validator, and base-aware change-record validation
- live or dev-integration verification: not claimed by this dormant support
  Landing Unit
- residual risk: v5 cannot authorize persistence or a new session until the
  explicit activation sequence completes

## Follow-Up

- required follow-up: complete the Security delta review, activate v5 in
  Workspace Governance and both consumers, deploy the consumer revisions,
  inventory non-pristine OOS sessions, and supersede the Epic #1203 packet
- owner: `workspace-governance`
- due date or closure condition: all Architecture Packet v5 activation gates
  are evidenced and the Epic #1203 packet is persisted under current v5

## Security Evidence

The change narrows evidence attribution to one declared Landing Unit and
readiness phase. It keeps v5 staged read-only and grants no caller, credential,
approval, source-merge, readiness, deployment, or ART-close authority.

## Rollback

Revert the v5 consumer logic, copied contract bundle, tests, documentation, and
this record together. Existing v1-v4 artifacts and sessions retain their
historical behavior.
