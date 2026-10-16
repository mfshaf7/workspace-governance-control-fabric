---
security_evidence:
  review_areas:
    - delivery
    - runtime
  reviewed_artifacts:
    - packages/control_fabric_core/src/control_fabric_core/record_authority_freshness.py
    - tests/test_record_authority_freshness.py
  findings: []
  risks: []
  workstreams:
    - WS-007
  notes: Source-only read guard; the existing exact-composition Security decision must accept the coordinated Platform record-cache implementation before activation. No source review or policy gate is relaxed.
---

# Console Canonical Record Freshness

## Summary

Require bounded deployment freshness before canonical record reads can support
the Console's existing owner workflows.

## Classification

Approved owner maintenance supporting the existing final acceptance in
`openproject://work_packages/1298`; no new ART child. Landing Unit Decision:
`child_isolated_landing_unit`, id `maintenance-1275-console-journey-wgcf`.
## Ownership

WGCF owns read admission only. Platform owns snapshot refresh and runtime;
Workspace Governance and Studio retain canonical source and contracts;
Security retains the exact operating decision.

## Root Cause

The existing deployment-refreshed Git ref had no runtime freshness evidence.
Offline ancestry alone cannot prove that ordinary approved record merges are
visible. This is a deployment boundary extension, not blame for WGCF following
its previous read-only contract.

## Source Changes

The optional commissioned guard requires the same short-lived Platform
checkpoint consumed by OOS before current canonical snapshots are actionable.
It checks bounded regular-file content, owner/ref/revision, freshness and Git
ancestry without network access or source mutation. Existing pinned contracts,
schemas, receipt scope, replay and cross-owner boundaries remain unchanged.
Native Git tests prove record advancement, stale snapshot denial, dirty tree
denial, rewritten history, symlinks, malformed files and every wired reader's
fail-closed entry. Full owner tests, project validation and both runtime image
proofs remain required before source review.

## Artifact And Deployment Evidence

Source evidence is the exact reviewed PR, owner tests and image proofs. There
is no deployment evidence from this source-only change.

## Live Verification

Pending coordinated Platform commissioning and the real Console acceptance
exercise. Native tests do not substitute for current provider operating proof.

## Follow-Up

Land the Platform checkpoint producer, accept the final source set through the
existing Security composition decision/index, then activate and prove the same
Console journey. Preserve the recorded existing final acceptance blocker.

## Operating Gate And Rollback

This record grants no runtime activation. Platform must produce/refresh the
private snapshots, compose the flag and prove current real owner reads after
the exact source set is accepted by Security. Security's change-record index
must include this record before batch closure. Rollback removes the composed
flag/cache binding and returns the previous accepted runtime; it does not
rewrite owner records or delete historical receipts.
