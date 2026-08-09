# Governance Control Fabric Dev-Integration Profile

This is the `dev-integration` profile for the Workspace Governance Control
Fabric runtime.

Runtime boundary:

- local-k3s Deployment and Service managed by the shared platform runner
- local-k3s PostgreSQL StatefulSet and Service for fabric-local metadata
- profile-scoped MinIO/S3-compatible object storage with a dedicated PVC,
  API-only application credentials, and namespace-local network isolation
- WGCF-owned Temporal activity Deployment, using the dedicated worker image,
  rendered at zero replicas by default
- persistent local state root under `.dev-integration/governance-control-fabric/<operator>`
- read-only smoke for API, graph, validation-plan, receipt metadata, and seeded
  evidence-object digest reads
- no stage or prod deployment approval

This profile is the first real runtime-access path for the control fabric. It
uses the published WGCF API image in local k3s so downstream console/API
contract work can consume a system service today while stage/prod deployment
gates remain separate.

## What It Will Run

- control-fabric FastAPI service from the published WGCF API image
- bounded activity execution from the separately published WGCF worker image
- local k3s Service for operator and future console access
- local PostgreSQL for graph, receipt, readiness, and ledger state
- local MinIO for content-addressed Delivery ART evidence-custody proof
- a bounded `wgcf.validation-readiness.evaluate` activity worker after explicit
  activation
- workspace-governance contracts mounted or synced as read-only authority input
- local session artifacts that bind source repos, profile state, and smoke
  evidence

## Runtime Boundary

Runtime state model:

- `persistent`

Persistent is selected because the control fabric will hold session, graph,
receipt, ledger, and evidence-custody state during long-running governance work.
Shared smoke must remain read-only. If mutating ledger, receipt, or artifact
smoke is needed later, create a separate disposable companion profile instead
of writing test traffic into this persistent working lane.

The local object store is deliberately bounded:

- the API ServiceAccount receives a bucket-scoped access key through its own
  Kubernetes Secret
- the MinIO root credential is confined to the storage workload and explicit
  storage-maintenance jobs
- OOS and OpenProject receive no object-store credential
- the API credential can list, read, write, and retrieve explicit versions in
  the profile bucket but cannot delete objects
- `up` proves that a same-key overwrite leaves the receipt-bound version
  retrievable, restores the accepted payload as current, and records the
  accepted version ID in a version-qualified storage reference
- a credential digest on the API and storage Pod templates restarts only those
  workloads when operator-scoped credential material changes
- activation fails closed unless the routed Security evidence-custody review
  is declared by the profile, present in the workspace, and matches its pinned
  content digest
- storage-affecting lifecycle actions also fail closed until the active
  workspace registry carries the exact Platform acceptance, actions, and
  stage-handoff gates declared by this profile
- retention and deletion remain explicit lifecycle operations rather than
  automatic cleanup
- the local profile uses namespace-internal HTTP and a local-path PVC; it does
  not claim governed transport encryption or encrypted-at-rest storage
- `stage` and `prod` remain denied until workload identity, secret delivery,
  transport and at-rest encryption, retention, backup, restore, and Security
  acceptance are approved for those lanes

The current profile starts PostgreSQL as a local k3s StatefulSet, runs database
migrations from the WGCF image, starts the API as a local k3s Deployment,
exposes it through a ClusterIP Service, and writes the operator access details
to the profile state root. The Temporal worker remains at zero replicas unless
all of these are supplied together:

- `DEVINT_WGCF_TEMPORAL_WORKER_ENABLED=true`
- `DEVINT_WGCF_TEMPORAL_ACTIVITY_EXECUTION_AUTHORIZED=true`
- `DEVINT_WGCF_TEMPORAL_ACTIVATION_REVIEW_REF=<accepted-security-review>`

The worker process repeats those checks before connecting. It consumes only
`wgcf.validation-readiness.v1`, writes only WGCF-local evidence, and never owns
the aggregate workflow. Cross-namespace reachability to the platform-owned
Temporal frontend is a separate activation prerequisite. This profile does not
create a stage deployment.

## Operator Actions

Use the shared platform runner:

- `make devint-up PROFILE=governance-control-fabric`
- `make devint-status PROFILE=governance-control-fabric`
- `make devint-access PROFILE=governance-control-fabric`
- `make devint-smoke PROFILE=governance-control-fabric`
- `make devint-down PROFILE=governance-control-fabric`
- `make devint-reset PROFILE=governance-control-fabric`
- `make devint-backup PROFILE=governance-control-fabric`
- `make devint-restore PROFILE=governance-control-fabric`
- `make devint-promote-check PROFILE=governance-control-fabric`

`reset` requires `CONFIRM=reset-wgcf-evidence`. `restore` requires
`CONFIRM=restore-wgcf-evidence` plus `DEVINT_BACKUP_FILE` pointing to a backup
inside the operator-scoped profile state or reset archive.

New backups are written directly under the profile `backups/` directory so
confirmed reset can archive every recoverable bundle. A backup includes the
current object set, the exact bytes of every receipt-bound version, and the
receipt record that names that version. Object-store version IDs are
server-assigned and therefore are not claimed to survive destructive storage
rebuilds.

Before clearing profile state, confirmed reset moves existing evidence backup
bundles and manifests into the operator-scoped reset archive. Restore validates
the current allowed location, archive digest, every current object, and every
receipt-bound version before mutation. It then creates a new immutable version,
reissues the local receipt to that version, records an old-to-new supersession
map, and restores the backed-up current object. The original absolute backup
path remains provenance rather than recovery authority.

`up`, `smoke`, `down`, `backup`, `restore`, and `reset` refuse to run when the
workspace authority registry or its referenced Platform acceptance record is
missing or stale. `status` and `access` remain available for read-only operator
orientation while activation is denied.

## Smoke Scope

The shared smoke path stays read-only and proves:

- API readiness
- component inventory read
- authority contract load
- database migration
- validation planner dry run
- receipt and ledger metadata read
- profile-scoped evidence storage availability
- storage credential and network isolation
- live API and maintenance connectivity plus unselected-Pod network denial
- seeded object digest, accepted version ID, same-key overwrite preservation,
  and version-qualified storage receipt verification

Smoke must not write to governed stage or prod state. It must not mutate the
persistent working ledger unless a separate disposable companion profile is
approved for that purpose. Its transient allow/deny Jobs re-prove current CNI
enforcement and are removed without writing WGCF ledger or object-store state.

## Stage Handoff Checks

The governed `stage` handoff is not ready until it proves:

- API readiness
- component inventory read
- authority contract load
- database migration
- validation planner dry run
- receipt and ledger metadata read
- profile-scoped evidence storage availability
- storage credential and network isolation
- version-bound content digest and storage receipt verification
- content-address-preserving backup and receipt-rebinding restore
- explicit retention and deletion boundary
- governed encryption identity and Security approval

These checks must mirror `stage_handoff.required_checks` in `profile.yaml` and
the workspace registry entry.

## References

- `workspace-governance/contracts/developer-integration-policy.yaml`
- `workspace-governance/contracts/developer-integration-profiles.yaml`
- `workspace-governance/contracts/components.yaml`
- `workspace-governance/docs/work-home-routing-contract.md`
- [ART evidence custody and source provenance Security review](https://github.com/mfshaf7/security-architecture/blob/main/docs/reviews/components/2026-08-09-art-evidence-custody-and-source-provenance.md)
