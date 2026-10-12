# WGCF Owner Evidence Profile Coverage

## Summary

- Date: 2026-10-12
- Short title: Admit sandbox-runtime owner evidence without removing filesystem coverage
- Environment: Delivery ART configured-path preflight and owner evidence acquisition
- Severity: High

## Classification

- Type: corrective owner-repository maintenance
- Signal: `2026-10-12-wgcf-owner-evidence-profile-regression`
- Owner: `workspace-governance-control-fabric`
- Related ART slice: Delivery work item `#1254` under Epic `#894`

## Root Cause

WGCF's accepted-base evidence profile exposed its isolated owner validation and
test runner only at `filesystem` fidelity. The approved product-publication
architecture requires WGCF to prove its positive and negative readiness cases
at `sandbox-runtime` fidelity, so OOS correctly stopped configured-path
preflight with `delivery_art_evidence_profile_incomplete` before creating a
source session.

The earlier coverage control proved that WGCF had a base-owned profile and
that its then-current filesystem and real-git cases were represented. It did
not preserve a sandbox-runtime binding for later admitted WGCF work.

## Change

The base-owned profile now exposes the existing isolated owner validation and
test runner at both `filesystem` and `sandbox-runtime` fidelity. The bindings
are separate so OOS executes only the command applicable to the current
architecture cases. The existing real-git source-diff binding is unchanged.

No product-publication implementation, authority contract, runtime state, or
ART work state is changed by this maintenance Landing Unit.

## Validation

- the profile contract test proves the filesystem, sandbox-runtime, and
  real-git bindings independently;
- the repository project validator and full unit suite must pass;
- OOS configured-path preflight for work item `#1254` must become ready after
  this maintenance change is merged into WGCF `main`.

## Rollback

Revert this maintenance pull request. That restores the prior profile and
intentionally blocks WGCF work whose architecture requires sandbox-runtime
owner evidence.
