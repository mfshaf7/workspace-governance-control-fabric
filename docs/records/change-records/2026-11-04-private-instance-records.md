---
security_evidence:
  review_areas: [identity, delivery, runtime]
  reviewed_artifacts:
    - packages/control_fabric_core/src/control_fabric_core/instance_records.py
    - packages/control_fabric_core/src/control_fabric_core/instance_record_reads.py
    - packages/control_fabric_core/src/control_fabric_core/instance_repository_intake.py
    - packages/control_fabric_core/src/control_fabric_core/instance_repository_inventory.py
    - packages/control_fabric_core/src/control_fabric_core/repository_readiness.py
    - contracts/repository-readiness/repository-readiness-request.schema.json
    - contracts/repository-readiness/repository-readiness-receipt.schema.json
    - contracts/repository-readiness/manifest.json
    - packages/control_fabric_core/src/control_fabric_core/artifact_registry.py
    - packages/control_fabric_core/src/control_fabric_core/db/models.py
    - apps/api/src/wgcf_api/app.py
    - migrations/versions/0016_instance_records.py
    - contracts/workspace-active-inventory/manifest.json
    - contracts/workspace-active-inventory/workspace-active-inventory.yaml
    - contracts/workspace-active-inventory/workspace-inventory-lifecycle.yaml
  findings: []
  risks: []
  workstreams: [WS-003, WS-005]
---

# Installation-private operational record foundation

## Summary

Add private database persistence and authenticated reads for accepted installation
records. This follows the operator's approved internal-storage choice and does
not activate registration writes or claim completed private-source support.

## Classification

Operator-authorized maintenance, no new ART or improvement candidate. Landing
Unit `maintenance-private-instance-fabric`, `feature_single_landing_unit`, based
on fetched `d3516e570aec812a3cd66933387d89b6f1fc94e4`. Unpublished batch.

## Ownership

Governance owns policy and record authority semantics; WGCF implements private
storage and validated runtime adapters; OOS owns operator decisions and workflow;
Platform owns database deployment, runtime configuration and credentials;
Security reviews this boundary; Console projects its actual authority.

## Root Cause

Existing runtime record snapshots copy canonical public Git data. They do not
provide a private authority for project metadata. A private project can be in
custody while registration would still disclose its identity in public source.

## Source Changes

Three tables atomically retain current records, immutable accepted revisions and
full original command/idempotency evidence. Exact version/digest comparison,
provider identity uniqueness, server-owned transition validation and integrity
readback are required. Replays return the original durable result after later
changes. Missing or altered evidence fails closed.

The read API admits the existing authenticated governance reader or OOS read operation, with fixed installation
configuration, one-query paginated register listings and byte-bounded history.
Only a successful installation-bound response can establish absence. An old or
unavailable backend cannot silently become an empty register. No generic client
write endpoint or migration-on-import is provided. The bounded registration
acceptance endpoint uses a distinct OOS-only operation; the read-only reader and
reconciler cannot accept decisions. Native Governance policy validates the actual
accepted request and current state, and the operation retains its full readiness
and original command. It replays a committed result before evaluating newer state.
OOS custody/operator binding and actual owner-controls attestations remain
explicit upstream authorities; browser flags are not those attestations.

A read-only source-authority projection evaluates the current internal record
through native Governance source-owner eligibility. Exact installation, version,
record digest, policy and unique provider are checked before projection, then
reread for changes. Proposed/admitted Intake permits bootstrap only; ordinary
source and verification require active source-only Inventory. Suspended, retired,
out-of-scope and control/Security-bound records remain denied. Legacy source is
selected only by positive canonical legacy proof. Projection grants no token,
checkout, source write, model access or runtime activation.

Native Inventory acceptance reuses the same transaction and original-result
recovery, with current Governance domain producers and lifecycle rules. Promotion
advances the existing Intake aggregate and retains its actual lineage. Updates,
suspension, retirement and restoration preserve physical source-only identity;
restoration binds the latest suspension/retirement receipt. Exact requests retain
operator acceptance, reason/impact where applicable and policy-bound readiness.
No new CI or app-acceptance requirement is invented for registration metadata.

A separate bounded catalog page supplies current metadata, matching aggregate
counts and explicit continuation. Search and inventory/eligible selection run in
the database with literal bound parameters. Listing and counts require two
queries independent of the number of returned projects; history is not scanned.

## Artifact And Deployment Evidence

The optional Platform source eligibility reader extends the existing method-scoped
authorizer with a distinct default-off caller/secret binding. Its sole operation
is `read-instance-source-authority`, not generic record reads or acceptance.
Authorization tests prove source eligibility succeeds while list, detail,
history, preparation, original-outcome, registration and reconciliation remain
denied. Partial or reused bindings fail closed. No live identity is commissioned
by this source preparation.

The provider-ID source lookup uses the installation's unique physical repository
key and returns the same eligibility proof after resolving its canonical alias.
It checks legacy overlap and current policy before/after the read; it never
scans per-repository history or derives canonical names from GitHub names.

Changes remain uncommitted and unpublished. No migration, live activation,
credential permission change, provider write or actual registration was run.
The policy contract is prepared in the Governance owner worktree. Source order
is Governance policy before dependent WGCF/OOS source and eventual Platform and
Security acceptance. Pending source has no claimed merged commit or operating
acceptance. Gary author preparation is recorded and its credential revoked.

## Live Verification

Isolated database and authenticated API tests cover restart/replay, transaction
rollback, stale/conflicting commands, cross-installation reads, provider identity,
tampered/missing evidence, bounded history and constant-query register reads.
The OOS reader checks the same receipt schema and canonical content digests.
This does not replace PostgreSQL activation proof or a complete native Console
registration/setup/admission/source-work exercise.

## Follow-Up

Canonical decision/readiness validation, internal registration writes and current
source projections are implemented and locally qualified. Review and land the
coordinated source, then separately accept its exact composed activation and
prove the normal Console operating route and interrupted recovery. Rollback
disables the new route and preserves all private
records and evidence; it must not drop populated tables or publish records to Git.


Selected repository status reads accept at most 25 exact ordered names and use
one installation-scoped joined query over the current record, revision and
original accepted operation. The response binds the complete requested identity
list, including missing native records, and returns compact state/version/provider
metadata without descriptions or history. Authentication and ordinary read-only
roles are unchanged; the narrow Platform source reader cannot use this endpoint.
Console uses the same status for Repo Control and primary attention rather than
reopening admission from old custody receipts. Unknown routes and failed reads
remain unavailable, never empty successful registration.

The existing Delivery repository-readiness receipt now accepts an exact internal
authority alternative. WGCF independently reevaluates current native ordinary
source eligibility. The effective rule is the actual accepted source-authority
policy URI/digest, not a fabricated public per-project rule. Mismatched versions
or installations produce durable non-ready evidence; unavailable or ineligible
source blocks fresh issuance. Receipt replay preserves actual authority, and OOS
reevaluates before linking. The existing Catalog reference remains unchanged.
Isolated native database tests prove issuance, durable replay, foreign/stale
rejection and suspension, alongside the unchanged legacy readiness suite.

## Legacy Inventory policy dependency reconciliation

Controlled activation exposed two stale policy copies in the retained Git-backed
Inventory bundle. Native registration clarified authority in Governance active
Inventory and lifecycle policies; the existing byte-equality guard correctly
rejected the old copies against current committed authority. Refresh these exact
files and their existing manifest to Governance commit
`c824d357c05eaa50da5c71180506fba10b8052f1`. The unchanged operation contract and
schema remain bound by their original content hashes, architecture and reviewed
operation scope. No source check is relaxed, no record is migrated and no
activation generation is recreated. OOS consumes the matching reviewed bundle
after this owner merge; final Security image/source selection follows its actual
hosted artifacts. Validate the real current committed Inventory snapshot before
publication, alongside the existing legacy/native and packaged-contract suites.
