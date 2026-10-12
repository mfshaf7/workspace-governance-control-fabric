---
security_evidence:
  review_areas:
    - identity
    - delivery
    - runtime
  reviewed_artifacts:
    - contracts/product-publication/manifest.json
    - tests/test_product_publication_readiness.py
  findings: []
  risks: []
  workstreams:
    - WS-007
  notes: Security approved the exact Product Publication readiness boundary for internal-only, loopback, single-operator dev-integration before this source gate was enabled.
---

# Product Publication Readiness Activation

## Summary

WGCF enables the source-owned Product Publication readiness gate after Security
work item `#1257` approved the exact reviewed boundary. This is corrective
owner-repository maintenance linked to existing Platform item `#1258`; it does
not create another ART child or activate a deployed runtime.

## Classification

- type: planned owner-repository source activation
- work home: maintenance linked to `openproject://work_packages/1258`
- Landing Unit Decision: `child_isolated_landing_unit`
- runtime impact: none until Platform composes the exact merged revision

## Ownership

- source gate and readiness runtime: `workspace-governance-control-fabric`
- Security acceptance: `security-architecture`
- workflow consumer: `operator-orchestration-service`
- deployment, configuration, operating proof, and rollback: `platform-engineering`

## Root Cause

The gate was deliberately false while Product Publication awaited independent
Security review. Security `#1257` is now merged and explicitly authorizes this
bounded activation step, so retaining the false source flag would incorrectly
block the approved Platform commissioning sequence.

## Security Decision

- URI: `security-architecture://product-publication/decisions/console-internal-v1`
- revision: `3dc6e0f6cc2f829b4967ac73269a409e18e0de21`
- review-file digest:
  `sha256:d1d120e1a081d29319394f70fc31b423f81039ccef9e2da0b87327241f5ba837`
- accepted scope: internal-only, loopback-only, single-operator,
  synthetic-data `dev-integration`

## Source Changes

- changes only the Product Publication `runtime_activation` source gate from
  `false` to `true`;
- preserves exact Workspace Governance authority revisions and digests,
  authenticated OOS caller scope, committed owner-manifest validation, durable
  readiness ledger, caller-scoped readback, fifteen-minute expiry, and all
  cross-boundary denials; and
- keeps Platform configuration mandatory for authority roots, runtime profile,
  service identity, implementation revision, database, and endpoint exposure.

## Validation

- the focused readiness suite must preserve positive, negative, replay,
  caller-isolation, expiry, tamper, manifest, and non-mutation coverage;
- the activation regression proves the source gate is true while missing
  Platform authority roots still fail closed; and
- the project validator and full repository suite remain required before
  merge.

## Artifact And Deployment Evidence

This change produces source evidence only. It does not deploy WGCF, create a
route, deliver a credential, write the readiness ledger, or prove operating
acceptance. Platform `#1258` must bind the exact merged activation revision and
perform the approved synthetic live proof.

## Live Verification

No live claim is made. The source gate changes build eligibility only; WGCF
still requires complete Platform-owned authority roots, runtime profile,
service identity, implementation revision, database, caller credential, and
endpoint composition before it can issue readiness.

## Follow-Up

Land the separate OOS source activation against the same Security decision,
then let Platform `#1258` compose and prove both exact activated revisions. Any
broader posture, caller, exposure, environment, or data class requires another
Security delta.

## Rollback

Immediate runtime rollback remains Platform-owned: disable the composed
capability and remove its runtime credentials. Source-level revocation reverts
this change so the WGCF Product Publication readiness builder again fails at
the embedded gate. Neither path deletes durable evidence.
