# Product Publication Readiness

## Summary

- Date: 2026-10-12
- Environment: source and isolated API/database tests
- ART: Epic #894, Feature #922, child #1254
- Landing unit: `delivery-894-product-publication-readiness`
- Owner: `workspace-governance-control-fabric`

## Boundary

Workspace Governance owns Product Publication policy and active product
inventory. Product owners own committed manifests and source evidence. WGCF
evaluates exact committed truth and persists caller-scoped readiness evidence;
it does not publish, mutate inventory, write owner source, approve Security
posture, or activate runtime.

## Source Changes

- Pin the exact Workspace Governance Product Publication authority revision.
- Distinguish the product source snapshot from the later manifest custody
  revision and prove their Git ancestry.
- Validate exact committed manifest digest, owner authority, inventory binding,
  lifecycle eligibility, posture tuple, version, request identity, and replay.
- Expose authenticated issue and caller-scoped immutable readback routes.
- Persist readiness and ledger evidence through migration
  `0015_product_publication`.
- Keep runtime activation disabled pending Security #1257 and Platform #1258.

## Evidence

Focused sandbox-runtime tests use real temporary Git repositories and a
temporary SQL database. They cover ready, blocked, stale, exact retry,
conflicting identity reuse, caller isolation, tamper detection, request bounds,
non-mutation, and disabled runtime activation. The base-owned evidence profile
also supplies the required real-Git Landing Unit checks.

## Next Boundary

OOS #1255 consumes this readiness artifact, Console #1256 supplies and projects
owner publication state, Security #1257 reviews the trust boundary, and
Platform #1258 controls runtime activation. None of those authorities are
claimed by this source change.
