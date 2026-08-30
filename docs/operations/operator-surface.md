# Workspace Governance Control Fabric Operator Surface

## Purpose

This is the primary operator instruction surface for the future
Workspace Governance Control Fabric runtime.

The authoritative workflow contract lives in:

- <https://github.com/mfshaf7/workspace-governance/blob/main/contracts/governance-control-fabric-operator-surface.yaml>

This repo implements that contract. It does not redefine workspace policy,
deployment authority, security acceptance, or ART mutation behavior.

## Current State

The repo is still in bootstrap state. The operator surface is intentionally
defined before implementation so the first runtime code does not invent a
different workflow.

Only the bootstrap status, core-library source snapshot ingestion, manifest
graph ingestion, fabric-local graph persistence helpers, read-only graph query,
validation planning, core-library validation execution, receipt inspection,
operator-readiness decisions with local ledger events, lifecycle retention
planning and confirmed cleanup, compact metrics and correlation ids,
core-library policy admission, core-library runtime governance records,
core-library evidence projection surfaces, broker-context ART readiness
projection, ART evidence packet projection, and local-k3s dev-integration API
access are implemented now. Treat the remaining CLI commands and API routes
below as the minimum required interface contract for later slices, not as
currently available runtime commands.

## Authority Boundaries

- `workspace-governance` owns contracts, schemas, workspace-root guidance,
  routing rules, maturity rules, and generated governance artifacts.
- `workspace-governance-control-fabric` owns runtime implementation for
  validation planning, readiness evaluation, receipts, ledger events, API,
  worker, and CLI.
- `platform-engineering` owns deployment state, release gates, version pinning,
  promotion, and runtime adoption.
- `security-architecture` owns security standards, findings, review criteria,
  and security acceptance posture.
- `operator-orchestration-service` owns broker-backed operator workflows,
  OpenProject adapters, ART writes, blockers, Review Packets, and completion
  evidence transport.

The fabric may create local/runtime snapshots, plans, receipts, decisions, and
ledger events. It must route authority mutation to the owning system.

## Operator Flow

Use this flow for the currently implemented local CLI surface:

1. Check fabric status.
2. Inspect the manifest graph for the repo, component, or ART scope.
3. Inspect compact source snapshot status for authority refs, source kinds,
   missing sources, and local source roots.
4. Plan validation for a workspace, repo, component, ART, or changed-file
   scope.
5. Run the bounded local check and emit a receipt.
6. List receipt metadata instead of rereading raw output.
7. Inspect a specific compact receipt without reopening raw artifacts.
8. Evaluate local readiness for known workspace, repo, component, and operator
   surface targets under a named profile, recording a fabric-local ledger event.
9. Evaluate broker-owned ART context before mutation when the work is
   delivery-ART closeout or readiness sensitive.
10. Generate compact ART completion or Review Packet evidence packets from
   WGCF receipts.
11. Plan fabric-local retention before `.wgcf` artifacts, receipts, or ledger
   files grow too large.
12. Apply retention only with explicit confirmation, preserving ledger exports
   before compaction.
13. Inspect receipt metrics and correlation ids when tracing validation or
   readiness decisions.
14. Use artifact and ledger references for handoff or audit.

For Prototype-to-Delivery ingress, OOS submits the exact committed Prototype
Delivery packet to `POST /v1/readiness/prototype-ingress`. Treat `allow` as
readiness evidence only. WGCF does not mutate Prototype, Delivery, or ART, and
the reconciler has read-only access to the immutable receipt endpoint.

For a Delivery Catalog Owner Repo value, OOS submits the repository identity,
Catalog value key, and expected `contracts/repos.yaml` digest to
`POST /v1/readiness/repositories`. WGCF returns `ready`, `not_admitted`,
`retired`, `stale`, or `contract_mismatch` after checking that exact authority
version and the matching repo rule. Only `ready` includes the OOS reference.
WGCF does not create or change repositories and does not write Catalog state.

For repository custody, OOS submits the canonical
`repository_custody_request` to `POST /v1/readiness/repository-custody`. WGCF
validates its canonical digest, exact pinned policy reference, supported
readiness action, approval reference, and secret-free artifact references.
`link-existing` permits provider readback only. `provision-new` additionally
requires the approved GitHub organization scope and every explicit baseline
setting; its allowed decision returns those exact settings with
`create-provider` as the next action. Neither result mutates a provider or
custody. The immutable decision is retrieved from
`GET /v1/readiness/repository-custody/{decision_token}`.

For repository lifecycle work, OOS submits the canonical
`repository_lifecycle_request` to
`POST /v1/readiness/repository-lifecycle`. WGCF checks the exact custody policy
binding, immutable repository identity, current custody and provider versions,
impact counts and disposition, action-specific confirmations, reversal
evidence, and provider authority. An allowed decision identifies exactly one of
`apply-workspace-custody`, `archive-provider`, `unarchive-provider`,
`retire-workspace-record`, or `restore-workspace-record`. `defer` returns
`requires-action`; stale, conflicting, unauthorized, unsupported, unavailable,
or inconsistent requests fail closed. Every decision records
`downstream_mutation: none` and performs no mutation. The immutable decision is
retrieved from
`GET /v1/readiness/repository-lifecycle/{decision_token}`.

These routes remain disabled in the normal runtime until the upstream authority
and explicit deployment gate both activate it. Sandbox tests may inject the
service before activation. OOS owns the later command lifecycle, provider
or workspace readback, custody mutation, reversal, terminal receipts, and
immutable history.

The default operator output must be compact. Full validation output belongs in
artifacts referenced by receipts and ledger events.

## Required CLI Shape

```bash
wgcf status
wgcf graph query --scope repo:<name> --manifest <path>
wgcf graph query --scope component:<name> --manifest <path>
wgcf graph query --scope art:<delivery-id> --manifest <path>
wgcf sources snapshot --workspace-root <path>
wgcf plan --scope repo:<name>|component:<id>|art:<delivery-id>|changed-file:<path>|workspace --tier smoke|scoped|full|release
wgcf check --scope repo:<name>|component:<id>|art:<delivery-id>|changed-file:<path>|workspace --tier smoke|scoped|full|release
wgcf catalog plan --workspace-root <path> --scope <scope> --profile <profile> --tier smoke|scoped|full|release
wgcf catalog check --workspace-root <path> --scope <scope> --profile <profile> --tier smoke|scoped|full|release [--operator-approved]
wgcf receipts list
wgcf inspect --receipt <receipt-id-or-path>
wgcf metrics receipts
wgcf readiness --target workspace|repo:<name>|component:<name>|operator-surface:<id> --profile <profile>
wgcf agent-action evaluate --request <request.json> --current <current.json> --ledger <ledger.jsonl> --actor <actor>
wgcf lifecycle plan --profile developer|ci|enterprise
wgcf lifecycle apply --profile developer|ci|enterprise --confirm
wgcf art graph --context <broker-context.json>
wgcf art readiness --context <broker-context.json> --operation complete --target-item-id <id>
wgcf art evidence --receipt <receipt.json> --item <id> --changed-surface <summary> --summary <summary>
```

Future CLI shape:

```bash
wgcf run --plan <plan-id-or-file> --emit-receipt
wgcf ledger tail --limit <n>
wgcf explain --decision <decision-id>
```

Required CLI behavior:

- default to compact human-readable summaries
- support `--json` for implemented automation surfaces
- return receipt, artifact, and ledger references for full evidence
- deny or block readiness when authority truth is unknown or stale
- block ART readiness when broker context shows a `Target PI`/`Iteration`
  mismatch, unless the iteration is an explicitly allowed `Program-wide / ...`
  label
- block completion of the last open `User story` or `Defect` child when its
  parent Feature is missing closeout-ready narrative headings
- avoid printing raw validation dumps unless explicitly requested

## Dev-Integration Evidence Storage

The active `governance-control-fabric` dev-integration profile adds one
namespace-local S3-compatible evidence store beside PostgreSQL. It is a local
custody proof, not governed stage or production storage.

Use the shared Platform runner from `platform-engineering`:

```bash
make devint-up PROFILE=governance-control-fabric
make devint-status PROFILE=governance-control-fabric
make devint-smoke PROFILE=governance-control-fabric
make devint-backup PROFILE=governance-control-fabric
make devint-restore PROFILE=governance-control-fabric \
  BACKUP_FILE=<operator-scoped-backup> \
  CONFIRM=restore-wgcf-evidence
make devint-down PROFILE=governance-control-fabric
make devint-reset PROFILE=governance-control-fabric \
  CONFIRM=reset-wgcf-evidence
make devint-promote-check PROFILE=governance-control-fabric
```

`down` preserves PostgreSQL and object-storage PVCs. `backup` records current
objects and exact receipt-bound bytes without credentials, and writes only
under the reset-archived profile backup directory. `restore` validates the
archive and every object against its manifest before mutation. Validation and
restore consume the same kernel-sealed, descriptor-bound archive and manifest,
so path replacement or in-place writes cannot change the accepted bytes during
the transaction. Restore captures a pre-restore backup when the receipt-bound
live version is present, creates new server-assigned object versions, rebinds each
receipt to its new immutable version with an explicit supersession map, and
then proves restored content addresses. The restore receipt identifies its
source as `sha256:<archive-digest>`; verify the retained bundle against that
value using its adjacent manifest's `archive_sha256` or `sha256sum` rather than
relying on the operator-selected path. `reset` is destructive and fails closed
without the exact confirmation above. Before clearing profile state, confirmed
reset validates every backup against its adjacent manifest, then preserves the
complete backup directory in the operator-scoped reset archive with one atomic
rename so the documented restore path remains usable after interruption. If the receipt-bound live version is gone,
restore skips the pre-restore backup only after proving the entire live bucket
has no versions and records that empty-store state in the restore receipt.

Activation also fails closed unless the profile carries the routed
`security-architecture` evidence-custody review and that review exists in the
landed `origin/main` history with the pinned content digest. The `up` action proves the API and
maintenance allow paths and an unselected-Pod denial against the live storage
Service. Storage-affecting lifecycle commands also require the
`workspace-governance` profile entry at the owner profile's pinned commit and
content digest to be landed on `origin/main` and to carry the exact Platform
acceptance, actions, and handoff checks declared by the owner profile. This
keeps a merged owner implementation dormant until landed workspace authority
activates the same contract and the pinned, landed Platform acceptance commit
matches its declared digest. The API receives only the bucket-scoped application
credential; the storage root credential remains limited to the storage and
named maintenance workloads. The root-user and application access-key names are immutable;
rotation changes both secret values together and retains the prior pairs in a
temporary namespace Secret until both revocation probes pass. The `up` action uses that
application identity to prove a same-key
overwrite cannot make the accepted bytes unreachable, restores the accepted
payload as current, and writes a receipt bound to the accepted object version
ID. Shared smoke verifies that pinned version without mutating storage and
proves the application identity cannot delete either the current object or the
receipt-bound object version.
It also recreates and removes bounded network-probe Jobs so each smoke run
proves current CNI enforcement rather than trusting an earlier `up` artifact.
Backup, restore, and smoke fail closed while a credential-retirement Secret
remains pending, so no evidence capture, recovery mutation, or read-only success
can bypass an interrupted rotation's denial proof.

## Temporal Activity Worker

The worker exposes a connection-free diagnostic:

```bash
wgcf-worker status
```

This is not a separate governance workflow command and does not expand the
authority contract. It proves the owner adapter, task queue, activity
registration, and activation posture without contacting Temporal.

The only implemented activity is:

- activity: `wgcf.validation-readiness.evaluate`
- queue: `wgcf.validation-readiness.v1`
- definition: `validation-readiness-run` version `1`
- validation: `component:workspace-governance`, `smoke`,
  `local-read-only`
- readiness target: `repo:workspace-governance-control-fabric`

Failure semantics are split by whether bounded evidence was produced:

- `blocked`: validation or readiness reached a terminal governance decision
- `timed-out`: a bounded validator exhausted its configured command timeout
- `unavailable`: bounded execution proved a required local execution
  capability was unavailable
- `WGCF_CONTRACT_REJECTED` and `WGCF_IDEMPOTENCY_CONFLICT`: non-retryable
  activity failures
- `WGCF_ACTIVITY_TIMED_OUT`, `WGCF_ACTIVITY_UNAVAILABLE`, and
  `WGCF_ACTIVITY_RETRYABLE`: retryable pre-result activity failures
- cancellation: propagated as native Temporal cancellation only after the
  isolated owner process group has stopped or completed

Temporal failure messages are stable and omit raw exception details. OOS owns
retry limits, activity timeouts, and terminal run projection. The activity
adapter heartbeats every two seconds and runs synchronous validation in an
isolated process group with a four-minute spawn-and-execution limit. Owner
evidence remains in an attempt-specific staging root until the complete process
group is confirmed absent; only then does an atomic rename grant canonical
evidence authority. Receipt artifact references are pre-bound to that committed
root, so promotion preserves their custody paths and digests. Cancellation
cannot interrupt the bounded stop-and-confirm task itself. Cancellation,
timeout, or unconfirmed termination leaves the attempt quarantined, so a retry
cannot overlap canonical evidence writes.
Aggregate ordering also requires OOS to schedule the activity with Temporal's
`WAIT_CANCELLATION_COMPLETED` policy, no heartbeat timeout, and a five-minute
start-to-close window that outlives WGCF's four-minute limit, five-second TERM
grace, and five-second group-exit confirmation; WGCF does not own those
settings. Heartbeats exist for cancellation delivery, not as proof that owner
execution stopped. When both sides are present, a cancelled or timed-out OOS
attempt cannot release a retry that shares canonical mutation authority with an
earlier WGCF owner.

Operator Orchestration Service owns the aggregate workflow, run state, retry
policy, and operator projection. WGCF owns only this bounded activity and its
fabric-local receipt, artifact, ledger, and idempotency records. The Temporal
payload contains no local path or raw validator output.

`wgcf-worker run` refuses to connect unless the worker is enabled, activity
execution is explicitly authorized, and a fresh Security activation review
reference is supplied. A successful `status` command is source-readiness
evidence only.

### Controlled commissioning proof

The first-runtime commissioning path is deliberately separate from normal
worker activation:

```bash
wgcf-worker controlled-proof status --repo-root .
```

This command is connection-free. It reports whether the controlled worker
source exists and whether an exact permit-derived owner context would authorize
startup. `wgcf-worker controlled-proof run` is denied unless all of these agree:

- `WGCF_CONTROLLED_PROOF_ENABLED=true`
- `WGCF_CONTROLLED_PROOF_EXECUTION_AUTHORIZED=true`
- raw context file and `WGCF_CONTROLLED_PROOF_CONTEXT_DIGEST`
- source revision baked into the worker image and matching the owner context
- writable durable evidence storage at the default controlled-evidence root or
  `WGCF_CONTROLLED_PROOF_EVIDENCE_ROOT`
- controlled Temporal address, namespace, task queue, and worker identity
- unexpired authorization and commissioning-session bindings in the context

The controlled worker polls only
`wgcf.controlled-proof.validation-readiness.v1`. It accepts only OOS activity
requests whose authorization, session, scenario execution, operator, source,
Temporal metadata, and required WGCF receipt ownership match the mounted
context. Reuse of an idempotency key across a different context or request is a
non-retryable mismatch.

WGCF executes the existing bounded validation/readiness owner path and leaves
its normal compact activity result unchanged. Separately, it records one
reference-only owner receipt under the controlled evidence root. The receipt
binds the permit, approvals, consumption evidence, session, scenario, actual
activity id, and bounded observation/result digests. It is not activation,
promotion, or post-run Security acceptance.

The controlled worker reads source identity only from the root-owned
`/opt/wgcf/build/source-revision` image file; deployment environment cannot
override it. It revalidates its context and image provenance while polling and
before a successful receipt commit. Cancellation is acknowledged only after the
owner process group is confirmed absent. Context revocation, expiry, identity
drift, queue drift, source drift, an unusable evidence root, or unconfirmed
process termination fail closed.

## Dev-Integration API Access

The first operator-access path is the `governance-control-fabric`
dev-integration profile. It deploys local PostgreSQL plus the published WGCF
API image into local k3s, runs database migrations, exposes a ClusterIP
Service, and writes access, health, readiness, database-migration, and smoke
artifacts under `.dev-integration/governance-control-fabric/<operator>`.

```bash
make -C /home/mfshaf7/projects/platform-engineering devint-up PROFILE=governance-control-fabric
make -C /home/mfshaf7/projects/platform-engineering devint-status PROFILE=governance-control-fabric
make -C /home/mfshaf7/projects/platform-engineering devint-smoke PROFILE=governance-control-fabric
make -C /home/mfshaf7/projects/platform-engineering devint-access PROFILE=governance-control-fabric
make -C /home/mfshaf7/projects/platform-engineering devint-down PROFILE=governance-control-fabric
```

This path is intentionally local dev-integration. It is not a stage deployment
and does not create a governed platform PostgreSQL instance. The profile
renders the activity worker at zero replicas until the activation gates above
are satisfied.

## Required API Shape

The API is now available for local dev-integration contract iteration. Do not
deploy it to governed stage or prod until platform and security gates approve
that runtime posture.

Required route meanings:

- `GET /healthz`
- `GET /readyz`
- `GET /v1/status`
- `GET /v1/graph`
- `GET /v1/graph/query?scope=<scope>`
- `GET /v1/budgets`
- `POST /v1/lifecycle/retention-plan`
- `POST /v1/lifecycle/retention-apply`
- `GET /v1/source-snapshots/status`
- `POST /v1/validation-plans`
- `POST /v1/validation-runs`
- `GET /v1/receipts`
- `GET /v1/receipts/{receipt_id}`
- `GET /v1/metrics/receipts`
- `POST /v1/readiness/evaluate`
- `POST /v1/agent-actions/evaluate`
- `POST /v1/art/graph`
- `POST /v1/art/readiness`
- `POST /v1/art/evidence-packet`
- `POST /v1/artifacts/delivery-art`
- `GET /v1/artifacts/delivery-art/{digest_hex}`
- `POST /v1/artifacts/delivery-art/{digest_hex}/reconcile`
- `POST /v1/readiness/delivery-art`
- `GET /v1/readiness/delivery-art/{receipt_token}`

Future route meanings:

- `POST /v1/source-snapshots`
- `GET /v1/ledger/events`
- `GET /v1/decisions/{decision_id}/explain`

No required route mutates upstream authority stores.

## Governance Operations Console Readiness

The future Governance Operations Console must be built on stable WGCF API
semantics, not private UI-only assumptions.

Readiness criteria live at:

- [../architecture/governance-operations-console-readiness.md](../architecture/governance-operations-console-readiness.md)

The console may read compact status, graph, validation, receipt, readiness,
ledger, and escalation state after those routes are implemented and approved
for deployment. It must not become an authority source, bypass broker-owned ART
mutation, or expose raw artifacts without an approved artifact custody path.

No dashboard implementation is part of the current operator surface.

## Context Governance Gateway Packet Seam

The future Context Governance Gateway is a separate packet producer. WGCF
consumes packet metadata, receipt refs, digests, and readiness signals; it does
not capture raw context or implement the gateway.

Integration seam:

- [../architecture/context-governance-gateway-integration.md](../architecture/context-governance-gateway-integration.md)

Default posture is deny raw model projection. WGCF evidence projections may
carry packet ids, producer receipt refs, digests, policy decisions, and safe
summaries only.

## Records Operators Should Expect

- `source-snapshot`
- `validation-plan`
- `validation-run`
- `control-receipt`
- `art-readiness-receipt`
- `art-evidence-packet`
- `evidence-projection`
- `runtime-governance-record`
- `agent-action-policy-decision`
- `readiness-decision`
- `retention-plan`
- `correlation-id`
- `receipt-metrics`
- `ledger-event`
- `authority-reference`
- `escalation-record`

Use receipts for operator-safe proof. Use ledger events for audit and handoff.
Use upstream PRs, ART records, platform records, and security reviews for their
own authority domains.

Agent-action evaluation is decision-only. The request and current-binding
files must be repo-local for CLI use; API use is restricted to the authenticated
OOS caller scope and binds the current caller to that authenticated identity.
An `allow` decision permits the downstream owner workflow to continue but does
not execute it. `deny` and `review-required` both block downstream execution.
Mutation also requires exact operator approval and a later owner receipt. Raw
context, model output, credentials, and owner-backend results remain outside
the decision and ledger records.

## Governance Manifests

The runtime manifest schema lives at:

- `schemas/governance-manifest.schema.json`

The manifest is an ingestion contract for future graph planning. It declares:

- repos
- components
- validators
- projections
- upstream authority refs

Operators should expect each manifest entity to cite `authority_ref_ids` or
`source_ref_ids`. A manifest that cannot point back to authority refs is not
ready for graph ingestion, because the fabric must not invent policy truth from
local implementation metadata.

The example manifest at `examples/governance-manifest.example.json` is valid
for scaffold testing and demonstrates the compact shape. It is not deployment
approval and not a replacement for workspace-governance contracts.

Current implementation can build an in-memory graph from a valid manifest,
query it through `wgcf graph query` or `GET /v1/graph/query`, build a validation
plan through `wgcf plan` or `POST /v1/validation-plans`, run bounded local
checks through `wgcf check` or `POST /v1/validation-runs`, list compact
receipts through `wgcf receipts list` or `GET /v1/receipts`, inspect one compact
receipt through `wgcf inspect` or `GET /v1/receipts/{receipt_id}`, and evaluate
known local readiness targets through `wgcf readiness` or
`POST /v1/readiness/evaluate`. The graph, plan, receipt, readiness decision,
and ledger records are fabric-local projections only; they do not mutate
authority stores.

Catalog-backed validation is the cutover path for workspace validator
invocation. `wgcf catalog plan` and `wgcf catalog check` consume the
workspace-owned governance validator catalog instead of requiring an operator
to hand-author a repo-local manifest. The catalog remains the authority for
command identity, concrete `wgcf_invocation` commands, representative scopes,
safety class, allowed profiles, and retirement posture. WGCF translates that
catalog into a runtime manifest, emits selected and suppressed catalog entries,
and blocks unresolved placeholders rather than guessing a command family.

Use these representative scopes for #536 shadow parity:

```bash
wgcf catalog check --workspace-root /home/mfshaf7/projects --scope component:workspace-governance --profile local-read-only --tier smoke
wgcf catalog check --workspace-root /home/mfshaf7/projects --scope component:delivery-art --profile dev-integration --tier scoped --operator-approved
wgcf catalog check --workspace-root /home/mfshaf7/projects --scope art:delivery-<id> --profile dev-integration --tier scoped --operator-approved
wgcf catalog check --workspace-root /home/mfshaf7/projects --scope component:platform-runtime --profile dev-integration --tier smoke --operator-approved
wgcf catalog check --workspace-root /home/mfshaf7/projects --scope component:security-review --profile local-read-only --tier smoke
```

`component:delivery-art` proves the broker and component-level health surface.
Use `art:delivery-<id>` when the evidence must include platform-owned scoped
OpenProject ART quality for one concrete initiative.

Direct validators stay available as rollback and source-authority entrypoints
until workspace-governance marks the relevant catalog register
retirement-eligible. Catalog-backed WGCF receipts can become normal operator
evidence only for scopes that pass the platform gate, current security review,
receipt parity, raw-output suppression, and direct-rollback requirements.

Validation planning uses four tiers:

- `smoke`: smallest declared checks for fast local confidence
- `scoped`: checks declared for the requested repo, component, validator,
  projection, authority, ART, or changed-file scope
- `full`: all manifest-declared validators for the current manifest
- `release`: full-surface planning plus current authority-ref freshness
  requirements

Changed-file planning accepts `changed-file:<repo-relative-path>` and expands
it to matching repo and component scopes declared by the manifest. This keeps
file-based planning deterministic without hardcoding validator policy in code.

Manifest validators may declare `reuse_policy.safe_to_reuse`,
`reuse_policy.freshness_seconds`, and
`reuse_policy.invalidate_on_authority_change`. When the planner receives
matching successful receipt records that are still fresh and still match the
validator authority-ref digests, it marks those checks as
`skip_fresh_receipt` candidates in the plan. If authority digests are missing or
changed and invalidation is enabled, the planner selects the validator to run
again and records the cache decision reason in the plan.

Manifest validators may also declare `execution_policy.timeout_seconds`,
`execution_policy.retry_count`, `execution_policy.output_budget_bytes`, and
`execution_policy.fail_on_output_budget_exceeded`. They may also declare
`execution_policy.invocation_class` and `execution_policy.max_duration_ms` so
the operator can see whether a check is intended for inline use, receipt
verification, a hard gate, checkpoint batching, or offline advisory analysis.
These are runtime execution controls only; they do not decide workspace policy.
Receipts record timeout, retry, output-budget, and invocation-budget decisions
as compact metadata while raw stdout/stderr remain in receipt-linked artifacts.

WGCF operation classes are deliberately tiered so the fabric does not become a
synchronous tax on every operator action:

- `inline-fast`: compact context/graph/planning reads such as ART continuation
  and graph query; these must stay bounded and paginated
- `receipt-check`: submit paths verify payload digests and fresh receipts
  instead of recomputing full validation
- `hard-gate`: completion, blocker, risk, and other irreversible mutations can
  fail closed before the owning adapter writes
- `checkpoint-batch`: projection sync, full quality, and broad graph work run
  at evidence checkpoints rather than on every draft mutation
- `offline-advisory`: new or unclassified future operations must be classified
  before becoming synchronous gates

CLI `wgcf budget show` and API `GET /v1/budgets` expose this budget contract.
Graph queries apply the `graph.query` page-size budget and return pagination
metadata with the compact result.

Execution policy also carries the first validator safety controls:
`execution_policy.profile`, `execution_policy.safety_class`,
`execution_policy.allowed_executables`, `execution_policy.allowed_roots`,
`execution_policy.allowed_env_vars`, and `execution_policy.blocked_env_vars`.
WGCF runs validators from a sanitized base environment, blocks secret-like
environment overrides unless explicitly allowlisted, blocks commands outside an
explicit executable allowlist, blocks repo roots outside `allowed_roots`, and
requires explicit operator approval for `network`, `privileged`, or
`host-control` safety classes.

The planner decision can be `planned`, `no_matching_validators`, or `blocked`.
It must explain selected checks, suppressed validators, and any operator-review
reason. Plan records also return `check_statuses` so operator and API surfaces
can distinguish `selected`, `suppressed`, `blocked`, `waived`, `stale`,
`failed`, and `external-owner-required` checks without parsing prose. The core
execution primitive must respect that decision. If the decision is blocked or
requires operator review, it emits a compact receipt without running validators.

Validation execution uses the schemas at:

- `schemas/validation-receipt.schema.json`
- `schemas/ledger-event.schema.json`

Current execution behavior:

- keeps bootstrap validation independent from WGCF receipts: the direct
  `scripts/validate_project.py` scaffold validator remains the bootstrap
  authority, while WGCF-produced receipts are runtime smoke evidence only
- runs only manifest-planned command checks
- runs with `shell=False` from the supplied repo root
- supports simple leading environment assignments such as `PYTHONPATH=...`
- runs with a sanitized base environment and explicit environment allow/block
  controls
- enforces manifest-declared command allowlists, allowed roots, safety classes,
  profiles, invocation classes, and output budgets before invocation
- writes full stdout/stderr to local artifact files for CLI and API callers
- does not publish validation-run command output to the Delivery ART artifact
  store because command output is not an approved registry artifact class
- includes only artifact refs, digests, byte counts, line counts, exit codes,
  duration, planner decision, and outcome in receipts
- includes a compact artifact custody summary in receipts with artifact ids,
  purposes, and a digest manifest, while keeping raw artifact bytes out of
  receipt and ledger records
- records per-check timeout, retry, output-budget, and performance-budget
  decisions in compact receipt metadata
- appends ledger events as JSONL through the core helper
- never embeds raw stdout/stderr in receipts or ART notes

CLI `wgcf check` and API `POST /v1/validation-runs` now compose planning plus
execution into a local receipt and ledger event. CLI `wgcf inspect` and API
`GET /v1/receipts/{receipt_id}` read compact receipt JSON only and refuse paths
outside the configured receipt directory. CLI `wgcf readiness` and API
`POST /v1/readiness/evaluate` evaluate only known targets and supported
profiles, then append a fabric-local ledger event for the readiness decision.
CLI `wgcf run --plan` and API-side database persistence wiring remain later
slices. The Temporal activity adapter exists but stays disabled until runtime
activation is accepted.

The dev-integration profile provisions versioned object storage, an API
workload identity, separate registry caller credentials, and PostgreSQL
metadata for the Delivery ART registry. Its shared seed and lifecycle probes
remain read-only storage proof; an authorized registry write plus a verified
custody receipt is the corresponding operating evidence. The registry may
persist only the artifact classes accepted by the routed Security review;
arbitrary command output remains out of scope.

Policy admission uses the schemas and policies at:

- `schemas/policy-decision.schema.json`
- `policies/opa/admission.rego`
- `policies/opa/validation_blocking.rego`
- `policies/opa/policy_ledger.rego`

Current policy behavior:

- evaluates repo/component admission subjects only
- requires owner repo and upstream authority refs before allow decisions
- blocks stale authority refs
- requires successful validation receipts unless validation is not required or
  a valid waiver is supplied
- records compact `policy.decision.recorded` ledger events linked to receipt
  refs where available
- does not define upstream workspace policy truth, security acceptance, or
  platform deployment approval

Runtime governance records use the schema at:

- `schemas/runtime-governance-record.schema.json`

Current runtime governance record behavior:

- records blocker and impediment decisions with owner, impact, decision path,
  evidence refs, and required next action
- records approval and waiver references with upstream authority refs
- records risk posture without accepting risk locally
- records change evidence links without copying raw runtime evidence into Git
- emits ledger events for runtime records
- always marks the authority boundary as `record-only-not-authority`
- does not mutate ART, OOS approval state, security findings, platform
  deployment state, or workspace-governance contracts

Evidence projection uses the schema at:

- `schemas/evidence-projection.schema.json`

Current projection behavior:

- projects control receipts into compact ART completion evidence fields
- projects control receipts into Review Packet item-evidence references
- projects control receipts into Git/change-record receipt and artifact refs
- includes policy decision ids when supplied by the caller
- never reads or embeds raw artifact content
- does not mutate ART, Review Packets, Git records, or upstream authority stores

## Delivery ART Artifact Registry

The Delivery ART registry is a bounded `dev-integration` custody surface. It
does not author artifacts, decide readiness, or mutate OpenProject. OOS remains
the producer and first semantic validator; WGCF independently verifies the
pinned contract, canonical content, and declared digest before persisting the
bytes through Platform-owned versioned storage. It records append-only metadata
and returns opaque artifact and custody-receipt references.

Approved classes are limited to:

- `delivery_art_architecture_packet`
- `delivery_art_work_start_record`
- `art_review_packet`

Architecture packets may use schema v1 or v2. The v1 shape remains readable
for compatibility. For artifact-bound readiness, WGCF validates the work
dependency graph, exact Landing Unit coverage and owner binding, source-backed
Landing Unit graph, and required human-gate references against the durable
artifact. Source landing order is therefore addressed by Landing Unit id,
including when one repository owns more than one Landing Unit. Invalid,
incomplete, cyclic, or ambiguous topology cannot receive a ready decision.

The API surface is:

- `POST /v1/artifacts/delivery-art` for OOS registration
- `GET /v1/artifacts/delivery-art/{digest_hex}` for OOS or WGCF retrieval
- `POST /v1/artifacts/delivery-art/{digest_hex}/reconcile` for the WGCF
  reconciler

Callers authenticate with `x-wgcf-caller-id` and
`x-wgcf-caller-secret`. OOS can register and read but cannot reconcile. The
WGCF reconciler can read and reconcile but cannot register. There is no general
operator credential and no delete route.

Registration input contains exactly:

```json
{
  "artifact_content": {},
  "content_digest": "sha256:<64-lowercase-hex>"
}
```

`artifact_content` is the source artifact before WGCF-generated digest and
custody fields. WGCF rejects duplicate JSON keys, floating-point values,
numbers outside the safe integer domain, unsupported classes, generated
custody fields, digest mismatch, and content over the bounded request limit.
A changed same-subject artifact must name the exact latest durable artifact in
`custody.supersedes`; retries with the same digest reuse the existing object and
receipt.

The successful response contains the reconstructed durable artifact, immutable
custody receipt, generation, and registry resolution. Public refs are opaque.
Endpoint, bucket, object key, object version, database identity, and secret
material must never appear in the response or ledger. Registration succeeds
before OOS may project references into ART. If later ART projection fails, OOS
retries projection against the existing digest; it must not compensate by
deleting custody evidence.

This implementation does not activate stage or production custody. OOS wiring
and OpenProject safe-reference projection remain outside this owner slice.

## Delivery ART Readiness Receipts

Use artifact-bound readiness only after OOS has authored and semantically
validated the relevant structured packet. This is separate from
`POST /v1/art/readiness`, which checks broker context before an ART mutation.

The issue route is:

- `POST /v1/readiness/delivery-art`

The read route is:

- `GET /v1/readiness/delivery-art/{receipt_token}`

OOS may issue and read receipts. The WGCF reconciler may read receipts but may
not issue them. Both routes use the same method-scoped caller headers as the
artifact registry. Requests identify one readiness level, exact Delivery and
work-item coverage, artifact identity, digest kind, and digest. Architecture,
implementation, and merge readiness resolve an exact durable registry ref.
Operating readiness carries the OOS pre-finalization candidate and a
`readiness-subject` digest; WGCF resolves its durable merge-ready predecessor
and source chain before deciding.

Outcomes are `ready`, `blocked`, or `review_required`. Only `ready` sets
`mutation_allowed` to true. The receipt remains advisory evidence consumed by
OOS: WGCF does not mutate ART, finalize Review Packets, register source
artifacts, approve a release, or accept security risk. Identical evaluation is
idempotent; changed evidence appends a superseding receipt generation. The
routes are source-complete for `dev-integration` only and do not activate stage
or production behavior.

## Database Foundation

The local runtime database stores only fabric-local implementation records:

- governance graph nodes and edges
- source snapshots
- validation plans and runs
- control receipts
- Delivery ART registry entries and custody receipts
- Delivery ART readiness receipts
- readiness decisions
- ledger events
- escalation records

The database does not become the source of truth for workspace contracts,
platform deployment state, security acceptance, or Delivery ART state. Those
remain owned by their upstream repos and systems. The fabric stores digests,
references, receipts, and decisions derived from those authorities.

Source snapshots are digest-only records for upstream authority files, repo
manifests, component contracts, and dev-integration profile files. The current
implementation can build these records in the core library and persist them
with authority digests, freshness markers, graph nodes, graph edges, and
synthetic scope nodes through the fabric-local SQLAlchemy model. CLI and API
wiring for those snapshots remain later slices.

Database configuration uses `WGCF_DATABASE_URL`. Operator status may display a
redacted database URL, but it must not print database passwords or raw
connection secrets.

## Worker Boundary

The worker implements one owner-scoped Temporal adapter. It validates an exact
request schema, rejects extra fields, serializes duplicate keys, returns the
prior result for an identical request, and fails on idempotency-key collisions.
It composes existing validation and readiness primitives rather than defining a
second policy path.

This is build-admitted source, not an active or production worker. The
dev-integration Deployment remains at zero replicas by default and later
activation must prove cross-namespace network policy, payload admission,
restart safety, WGCF cancellation acknowledgement paired with the compatible
OOS wait-for-completion policy, and fresh Security acceptance.

## Profiles

- `local-read-only`
  - local snapshots, validation plans, checks, receipts, and ledger events
- `dev-integration`
  - active local-k3s API and PostgreSQL runtime integration for local contract
    iteration
- `governed-stage`
  - future governed deployment posture after release and security approval

Unknown authority, stale source snapshots, failed shadow parity, missing owner
boundaries, required security deltas, and platform release gates must not pass
best-effort readiness.

## Blockers And Escalation

The fabric emits an escalation record when it cannot continue honestly.

Required blocker triggers:

- `unknown-authority-source`
- `stale-source-snapshot`
- `shadow-parity-failed`
- `missing-owner-boundary`
- `security-delta-required`
- `platform-release-gate-required`

Routing:

- Active ART impact routes through `operator-orchestration-service`.
- Missing workspace authority routes through `workspace-governance`.
- Security deltas route through `security-architecture`.
- Deployment, version, promotion, and runtime adoption gates route through
  `platform-engineering`.

## Denied Behavior

The fabric must not:

- mutate `workspace-governance` contracts directly
- mutate platform approved deployment state
- make security acceptance decisions
- mutate Delivery ART directly
- execute autonomous AI governance decisions
- hide raw validation output only in chat
- replace Review Packets for source-backed ART work
- treat compact output as full evidence

## Day-One Implementation Rule

The first implementation should be local-first and practical, but it must
preserve the workflow shape:

- source snapshot before validation plan
- validation plan before validation run
- receipt before operator success claim
- ledger event for meaningful fabric actions
- escalation record for blockers instead of silent pass/fail ambiguity

If implementation needs a new command, route, record, profile, or authority
meaning, update the `workspace-governance` contract first.
