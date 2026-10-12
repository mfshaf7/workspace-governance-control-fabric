# Delivery ART Architecture v6 Activation

Date: 2026-10-12

## Classification

- work home: owner-repo maintenance linked to the existing improvement candidate
- Landing Unit: `maintenance-delivery-art-v6-wgcf-activation`
- owner: `workspace-governance-control-fabric`
- tracking ref: `workspace-governance/reviews/improvement-candidates/2026-10-12-product-publication-activation-ownership-regression.yaml`

## Change

WGCF now admits Architecture Packet schema v6 as the sole current format for
new canonical custody and fresh architecture-readiness receipts. Schema v5 is
retained as immutable historical evidence for already-bound work.

The activated contract keeps the already-reviewed v6 semantics: every
`before_runtime_activation` gate must be represented by one exact runtime
activation chain that binds source owner, exact revision and field evidence,
source activation Landing Unit, and all dependent commissioning Landing Units.

## Boundary

This changes no trust boundary and grants no stage or production authority.
WGCF remains the canonical artifact and readiness custodian, OOS remains the
workflow author and OpenProject adapter, Workspace Governance remains contract
authority, Security remains acceptance authority, and Platform remains runtime
commissioning authority.

Activation is sequenced behind the refreshed Security delta review and ahead
of the Workspace Governance authority cutover. Existing v1-v5 packets remain
readable; no historical artifact is rewritten.

## Validation

- pinned contract-bundle validation
- artifact-registry current/historical admission tests
- Delivery ART readiness and activation-chain tests
- full repository test suite before review
