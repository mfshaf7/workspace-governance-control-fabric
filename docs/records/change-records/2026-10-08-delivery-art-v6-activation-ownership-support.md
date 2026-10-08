---
security_evidence:
  review_areas:
    - delivery
    - runtime
  reviewed_artifacts:
    - contracts/delivery-art/delivery-art-architecture-packet.schema.json
    - contracts/delivery-art/delivery-art-architecture-v6-activation-parity-vectors.schema.json
    - contracts/delivery-art/fixtures/architecture-packet-v6-activation-parity-vectors.valid.json
    - packages/control_fabric_core/src/control_fabric_core/delivery_art_contracts.py
    - packages/control_fabric_core/src/control_fabric_core/delivery_art_readiness.py
    - docs/operations/operator-surface.md
  findings: []
  risks: []
  workstreams:
    - WS-007
  notes: "The change adds dormant fail-closed v6 validation and readiness selection. Current v5 custody and architecture readiness remain unchanged; a separate Security delta review is an activation prerequisite."
---

# Delivery ART V6 Activation Ownership Consumer Support

## Summary

WGCF now understands the staged Architecture Packet v6 source-activation
chain. V5 remains the only current custody and architecture-readiness version
until independent OOS parity, Security review, session inventory, and
coordinated activation complete.

## Classification

- area: Delivery ART contract validation, artifact custody, and readiness
- type: corrective workflow and planning-integrity maintenance
- runtime impact: dormant v6 validation and readiness selection; no v6 custody
  or architecture-readiness activation

## Ownership

- owner repo: `workspace-governance-control-fabric`
- related improvement candidate:
  `workspace-governance/reviews/improvement-candidates/2026-10-08-delivery-plan-activation-ownership-regression.yaml`
- authority change:
  [workspace-governance#240](https://github.com/mfshaf7/workspace-governance/pull/240),
  merge `052261b14196d52318767e1bf371b1c156c0ac26`

## Root Cause

- immediate failure: an Epic plan routed directly from Security review to
  Platform commissioning while an OOS-owned embedded activation field remained
  false
- actual root cause: v5 models human gates and source order but does not bind
  the source owner, observed activation state, or required owner source change
- why it escaped earlier controls: acyclicity and temporal checks could prove
  that the declared graph was executable without proving that the graph
  contained the source-owned state transition needed by the runtime

## Source Changes

- pin the exact Workspace Governance v6 staged schema and shared activation
  parity vector
- validate exact source evidence, source ownership, separate activation and
  commissioning Landing Units, and Security-to-source-to-commissioning order
- apply v5 evidence-owner readiness selection to staged v6 artifacts
- keep the current version at v5 so v6 cannot enter custody or receive fresh
  architecture readiness

## Artifact And Deployment Evidence

- source-only change, or build/deployment evidence: source-only dormant
  consumer support
- image tag or digest: None
- runtime revision: None; activation is a separate governed Landing Unit

## Live Verification

- local validation: focused Delivery ART contract and readiness tests, full
  Python suite, project validator, and base-aware change-record validation
- live or dev-integration verification: not claimed by dormant support
- residual risk: v6 stays unsupported until the remaining activation gates are
  complete

## Follow-Up

- required follow-up: complete Security delta review, merge both consumer
  support changes, inventory active sessions, and activate v6 in all three
  owners before superseding the Epic 1203 architecture packet
- owner: `workspace-governance`, `workspace-governance-control-fabric`,
  `security-architecture`, and `operator-orchestration-service`
- closure condition: a fresh v6 packet rejects missing or wrongly ordered
  source activation and both consumers report v6 current

## Rollback

Revert the v6 consumer logic, copied contract bundle, tests, docs, and this
record together. Existing v1-v5 artifacts retain their prior behavior.
