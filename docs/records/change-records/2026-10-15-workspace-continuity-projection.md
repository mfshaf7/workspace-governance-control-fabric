# Workspace Continuity Projection

## Summary

- Date: 2026-10-15
- Environment: source and isolated API/database tests
- ART: Epic #1275, Feature #1293, child #1294
- Landing unit: `delivery-1275-registry-projections`
- Owner: `workspace-governance-control-fabric`

## Classification

This is accepted initiative work implementing the already-approved Workspace
Registry and Product Portfolio read-model capability. It extends the existing
authenticated Console read boundary; it does not introduce a new authority or
product.

## Ownership

- Workspace Governance owns canonical Registry, Intake, and lifecycle truth.
- WGCF owns the bounded, read-only continuity projection.
- OOS owns workflow and terminal Product Publication state.
- The Console consumes the projection and does not infer authority locally.
- Security and Platform retain activation and runtime composition authority.

## Root Cause

The Console previously had separate registry and publication-related surfaces
but no single canonical projection binding product identity, intake lineage,
current lifecycle state, and readiness freshness. UI-side joining would create
duplicate authority logic and permit stale readiness to appear current.

## Source Changes

- Add authenticated `GET /v1/projections/workspace-continuity` using the
  dedicated Console read credential.
- Read exact committed Workspace Governance inventory, intake, and append-only
  history through the existing pinned active-inventory authority.
- Join latest persisted Product Publication readiness without treating
  readiness as terminal publication.
- Mark expired or authority-mismatched readiness stale and non-actionable;
  represent missing readiness explicitly as unavailable.
- Fail closed on missing authority, identity collision, invalid lineage,
  history/current-record contradiction, corrupt readiness, or unavailable
  storage.
- Add a versioned response schema, focused positive and negative cases, API
  authentication coverage, and primary operator documentation.

## Artifact And Deployment Evidence

This Landing Unit creates source and validation evidence only. Runtime remains
default-denied behind `WGCF_WORKSPACE_CONTINUITY_PROJECTION_ENABLED`; later
Security and Platform work must approve and compose the exact merged revision.

## Live Verification

No live-operating claim is made here. An isolated integration check uses the
real committed Workspace Governance authority and proves that the current
registry projects without fixture data. Runtime commissioning belongs to the
planned Platform gate after Security acceptance.

## Follow-Up

Console child #1295 consumes the stable projection for Workspace Registry and
Product Portfolio list/detail continuity. Security #1303 then reviews the exact
composed boundary before Platform #1291 commissions it.

## Packaged-Runtime Repair

The live Epic #1275 acceptance run found that the installed Python module
resolved the response schema below `/usr/local` even though the image copied it
under `/app/schemas`. Repair Landing Unit
`delivery-1275-registry-projections-repair` now binds the exact packaged schema
path through `WGCF_WORKSPACE_CONTINUITY_SCHEMA_PATH`, tests both valid and
missing bindings, and keeps the same fail-closed 503 behavior for unavailable
contract truth.

## Rollback

Disable the Platform-owned runtime gate immediately if commissioned, then
revert this Landing Unit. Existing readiness tables, Workspace Governance
authority, and OOS workflows remain unchanged.
