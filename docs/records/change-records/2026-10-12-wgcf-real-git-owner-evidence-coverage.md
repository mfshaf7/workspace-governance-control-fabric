# WGCF Real-Git Owner Evidence Coverage

## Summary

- Date: 2026-10-12
- Short title: Admit merge-ready WGCF owner tests
- Environment: Delivery ART configured-path preflight and owner evidence acquisition
- Severity: High

## Classification

- Type: corrective owner-repository maintenance
- Signal: `2026-10-12-wgcf-real-git-evidence-coverage-regression`
- Owner: `workspace-governance-control-fabric`
- Related ART slice: Delivery work item `#1267` under Epic `#894`

## Root Cause

WGCF's accepted-base evidence profile exposed its isolated owner test runner at
`filesystem` and `sandbox-runtime` fidelity, but not at `real-git` fidelity.
The approved merge-ready conformance cases for work item `#1267` require both
test and validation evidence at `real-git` fidelity. OOS therefore correctly
stopped configured-path preflight before it created a source session.

The earlier compatibility check proved the then-required sandbox-runtime
coverage but did not prove every evidence-kind and fidelity pair in the later
approved architecture packet.

## Change

The base-owned profile exposes the existing isolated owner validation and test
runner at `real-git` fidelity in addition to its existing filesystem and
sandbox-runtime bindings. The source-diff validation remains a separate
real-git command, so merge-ready WGCF work supplies both required evidence
kinds without changing the evidence mechanism or runtime boundary.

No Product Publication implementation, authority contract, runtime state, or
ART work state changes in this maintenance Landing Unit.

## Validation

- the profile contract test proves filesystem, sandbox-runtime, and real-git
  test bindings independently;
- the repository project validator and full unit suite pass;
- OOS configured-path preflight for work item `#1267` becomes ready after this
  maintenance change reaches WGCF `main`.

## Rollback

Revert this maintenance pull request. That restores the prior profile and
intentionally blocks WGCF work whose conformance cases require merge-ready
real-git owner tests.
