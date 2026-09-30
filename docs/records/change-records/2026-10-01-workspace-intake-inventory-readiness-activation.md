# Workspace Intake and Inventory Readiness Activation

## Summary

- Date: 2026-10-01
- Short title: Bind Intake and Inventory readiness to the routine operation contract
- Environment: source and isolated API/database tests
- Severity: High

## Classification

- Type: bounded readiness source activation
- ART: Epic #1203, Feature #1205, child #1207
- Landing unit: delivery-1203-intake-inventory-readiness
- Owner: workspace-governance-control-fabric

## Boundary

Workspace Governance remains contract and canonical source authority. WGCF
evaluates Workspace Intake classification, Active Inventory promotion, and
Active Inventory lifecycle requests and persists immutable caller-scoped
receipts. It does not mutate canonical source, orchestrate review or merge,
project Console success, approve Security posture, or activate a deployment.

The source capabilities are active only under the exact merged #1206 operation
contract. Runtime construction remains gated by the `dev-integration` profile,
capability-specific environment admission, authority checkout, service
identity, implementation revision, and database. Security #1216 and Platform
#1217 must approve and compose those settings before routine availability.

## Source Changes

- Pin the exact #1206 operation contract, schema, authority commit, content
  digests, architecture packet, and capability set in both contract bundles.
- Validate that activation binding before either bundle can load.
- Activate the three source evaluators while retaining all runtime gates.
- Preserve non-mutating readiness behavior and immutable caller-scoped receipt
  custody.
- Cover incomplete configuration, exact configured construction, and tampered
  activation authority in focused tests.

## Rollback

Disable the capability-specific runtime environment settings and revert this
Landing Unit. Preserve issued receipts and ledger history. Runtime activation,
identity commissioning, revocation, and teardown remain later owner evidence.

## Remaining Gates

OOS #1208 must orchestrate the durable workflow, Console #1209 must project it,
Security #1216 must accept the exact merged revisions, Platform #1217 must
commission the composition, and #1210 must prove the composed operating path.
