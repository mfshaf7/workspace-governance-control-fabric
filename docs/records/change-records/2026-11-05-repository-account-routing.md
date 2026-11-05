---
security_evidence:
  review_areas: [identity, secrets, delivery, runtime]
  reviewed_artifacts:
    - contracts/repository-readiness/manifest.json
    - contracts/repository-readiness/repository-readiness-request.schema.json
    - contracts/repository-readiness/repository-readiness-receipt.schema.json
    - contracts/repository-readiness/repository-readiness-reference.schema.json
    - packages/control_fabric_core/src/control_fabric_core/repository_custody_readiness.py
    - contracts/repository-custody/manifest.json
  findings: []
  risks: []
  workstreams: [WS-003, WS-005]
---

# Connected GitHub account routing

## Summary

Evaluate personal and organization creation without changing provider or custody authority.

## Classification

Operator-approved repository maintenance outside an active ART initiative. No new
ART items or improvement candidates. Landing Unit decision
`child_isolated_landing_unit`, `maintenance-repository-accounts-wgcf`: owner,
review, validation and rollback boundaries require separate repository landings.

## Ownership

WGCF owns readiness evaluation; Governance remains policy authority. Console owns the operator interface, and Security owns acceptance.

## Root Cause

This is an approved capability extension of the initial organization-only route,
not a newly invented defect. Fixed organization input did not cover operators
who use both personal and organization repositories.

## Source Changes

One installation can connect personal accounts, organizations or both, choose a
default and retain one private Registry. A single eligible destination is automatic;
multiple destinations require an explicit form selection. Verified provider account
ID/type and account-specific grant bind every operation. Existing aliases and
retained requests keep their original identity; same-named repositories stay
separate by provider ID and qualified coordinates.

Metadata, provisioning, source authoring and lifecycle authority remain separate.
Lifecycle tokens retain their explicit one-repository bound. No browser secret,
ambient credential, cross-account fallback, new service or database is introduced.

Security delta:
[Connected GitHub accounts](https://github.com/mfshaf7/security-architecture/blob/main/docs/reviews/components/2026-11-05-repository-account-routing.md).
The main agent retains Security judgment and final landing review; this is not an
independent assessment. Gary authors source and the delegated human reviews/merges.

## Artifact And Deployment Evidence

The isolated source candidate is unpublished and unactivated. Existing live source
remains accepted and unchanged. Exact PR, base-aware validation and activation
receipts must be recorded during landing and operating qualification.

## Live Verification

Candidate validation covers both account types/default selection, denied/expired
or missing grant, exact binding, provider readback, same-name collision, original
request recovery, private selection, credential renewal/consumption and truthful
visibility. Full local CI-equivalent checks and browser review remain required
before publication; no operating completion is claimed by this source record.

## Follow-Up

Land canonical Governance first and refresh governing source pins. Land OOS
before updating WGCF’s readiness consumer pin to the actual merged OOS commit;
then land WGCF, Platform and Console in dependency order. Regenerate the Security
change-record index from landed owner main before claiming closure. Normal exact
composed Security/Platform acceptance must precede live activation. Roll back the
same source/connection boundary while retaining canonical requests and evidence.

Packaged onboarding is recorded in the existing Console release backlog: ask for
personal accounts, organizations or both after installation, guide provider-native
authorization and allow later default/connection changes. Building an installer
or declaring customer setup qualified is outside this maintenance batch.


## Onboarding consumer compatibility

The coordinated Registry onboarding repair reuses this owner's existing current
active Inventory readiness. Its schema identifiers adopt the same bounded
canonical names as operational registration, including qualified dot/underscore
names. Decision authority, immutable receipt, active posture, source identity,
version/digest and freshness checks are unchanged. Schema and consumer checksums
are refreshed together. The consumer commit remains a pending source-candidate
binding until OOS lands; replace it with the actual merged OOS commit before
this WGCF landing. Do not claim remote pin evidence from local candidate tests.

## Onboarding validation

The project scaffold validator and all592 owner tests pass. The added positive
case issues and rereads actual ready receipts for account-qualified identifiers
with dot/underscore names; existing native Inventory tests retain foreign-instance,
stale-version and suspension denials. This proves local candidate compatibility.
It does not prove the future merged OOS consumer pin or live activation.

## Exact producer and consumer publication bindings

The custody bundle pins merged Governance PR321 at
f5e06f3c6c480ffe3d48a79f24dfe84c8d0d0e8f. Readiness now pins its actual merged
OOS consumer PR408 at c0e66ca2333e7c239809930ca390018cf7623b50; the consumer
schema hash was compared with that merged Git object. All592 owner tests and
project/governance validators pass after the governing producer update. Exact
composed Security acceptance and Platform activation remain separate.
