# Prototype Maturity Readiness Activation

## Summary

- Date: 2026-09-10
- Short title: Activate Prototype Maturity readiness evaluation
- Environment: source and isolated API/database tests
- Severity: High

## Classification

- Type: bounded readiness activation
- ART: Epic #892, Feature #920, child #1129
- Landing unit: delivery-892-prototype-maturity-activation-readiness
- Owner: workspace-governance-control-fabric

## Boundary

Workspace Governance remains policy authority and Workspace Prototype Studio
remains source authority. WGCF only evaluates Candidate and Baseline Promotion
readiness and persists caller-scoped evidence. OOS owns workflow coordination
and source mutation, Platform owns identity and runtime commissioning, and
Security owns the activation decision.

This change activates the existing WGCF source capability only for the
`dev-integration` profile. It does not enable OOS orchestration, project a
credential, commission a runtime, apply a Prototype transition, create
Delivery work, publish a Portfolio entry, or grant stage or production
authority.

## Source Changes

- Pin the merged #1125 normal-availability review and its exact content digest.
- Pin the finalized #1100 composed-conformance Review Packet.
- Pin the finalized #1128 identity Review Packet and exact Platform identity
  definition.
- Require the complete activation evidence shape before loading the contract.
- Change the WGCF manifest source gate to active while preserving explicit
  `dev-integration` profile and environment admission in the runtime builder.
- Update focused tests and the primary operator surface.

## Evidence

Focused tests cover successful Candidate and Baseline readiness, immutable
caller-scoped persistence and readback, malformed activation evidence,
unauthorized callers, stale authority, changed policy or Security binding,
invalid digests, replay conflict, expiry, source-contract tampering, prohibited
content, and profile or environment activation denial.

## Remaining Gates

#1130 must activate the matching OOS orchestration source. #1131 may commission
the dedicated identity and composed runtime only after both source activations
merge. #1132 owns normal Console operating proof. WGCF activation alone is not
normal availability.
