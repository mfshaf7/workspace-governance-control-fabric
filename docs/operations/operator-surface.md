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
validation planning, core-library validation execution, core-library policy
admission, core-library runtime governance records, core-library evidence
projection surfaces, and local-k3s dev-integration API access are implemented
now. Treat the remaining CLI
commands and API routes below as the minimum required interface contract for
later slices, not as currently available runtime commands.

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
7. Use artifact and ledger references for handoff or audit.

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
wgcf receipts list
```

Future CLI shape:

```bash
wgcf run --plan <plan-id-or-file> --emit-receipt
wgcf inspect --receipt <receipt-id-or-path>
wgcf readiness --target workspace|repo:<name>|component:<name>|operator-surface:<id> --profile <profile>
wgcf ledger tail --limit <n>
wgcf explain --decision <decision-id>
```

Required CLI behavior:

- default to compact human-readable summaries
- support `--json` for implemented automation surfaces
- return receipt, artifact, and ledger references for full evidence
- deny or block readiness when authority truth is unknown or stale
- avoid printing raw validation dumps unless explicitly requested

## Worker Diagnostic Entry Point

The implementation also exposes a worker diagnostic entrypoint:

```bash
wgcf-worker status
```

This is not a separate governance workflow command and does not expand the
authority contract. It lets operators and CI prove the worker package is
packaged, Temporal-shaped, and still intentionally non-running.

Current worker constraints:

- no Temporal SDK dependency
- no connection to a Temporal service
- no task-queue polling
- no long-running workflow execution
- no upstream authority mutation

The worker status may show Temporal namespace, task queue, and address settings
from `WGCF_TEMPORAL_NAMESPACE`, `WGCF_TEMPORAL_TASK_QUEUE`, and
`WGCF_TEMPORAL_ADDRESS`, but it treats them as configuration shape only until a
future runtime slice activates the worker lane.

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

This path is intentionally local dev-integration. It is not a stage deployment,
does not create a governed platform PostgreSQL instance, and does not activate
the worker runtime.

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
- `GET /v1/source-snapshots/status`
- `POST /v1/validation-plans`
- `GET /v1/receipts`

Future route meanings:

- `POST /v1/source-snapshots`
- `POST /v1/validation-runs`
- `GET /v1/receipts/{receipt_id}`
- `POST /v1/readiness/evaluate`
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
- `evidence-projection`
- `runtime-governance-record`
- `readiness-decision`
- `ledger-event`
- `authority-reference`
- `escalation-record`

Use receipts for operator-safe proof. Use ledger events for audit and handoff.
Use upstream PRs, ART records, platform records, and security reviews for their
own authority domains.

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
checks through `wgcf check`, and list compact receipts through
`wgcf receipts list` or `GET /v1/receipts`. The graph, plan, receipt, and ledger
records are fabric-local projections only; they do not mutate authority stores.

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
`execution_policy.fail_on_output_budget_exceeded`. These are runtime execution
controls only; they do not decide workspace policy. Receipts record timeout,
retry, and output-budget decisions as compact metadata while raw stdout/stderr
remain in receipt-linked artifacts.

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

- runs only manifest-planned command checks
- runs with `shell=False` from the supplied repo root
- supports simple leading environment assignments such as `PYTHONPATH=...`
- runs with a sanitized base environment and explicit environment allow/block
  controls
- enforces manifest-declared command allowlists, allowed roots, safety classes,
  profiles, and output budgets before invocation
- writes full stdout/stderr to local artifact files
- includes only artifact refs, digests, byte counts, line counts, exit codes,
  duration, planner decision, and outcome in receipts
- includes a compact artifact custody summary in receipts with artifact ids,
  purposes, and a digest manifest, while keeping raw artifact bytes out of
  receipt and ledger records
- records per-check timeout, retry, and output-budget decisions in compact
  receipt metadata
- appends ledger events as JSONL through the core helper
- never embeds raw stdout/stderr in receipts or ART notes

CLI `wgcf check` now composes planning plus execution into a local receipt and
ledger event. CLI `wgcf run --plan`, API `POST /v1/validation-runs`, API-side
persistence wiring, and worker queue execution remain later slices.

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

## Database Foundation

The local runtime database stores only fabric-local implementation records:

- governance graph nodes and edges
- source snapshots
- validation plans and runs
- control receipts
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
synthetic scope nodes through the fabric-local SQLAlchemy model. CLI, API, and
worker wiring remain later slices.

Database configuration uses `WGCF_DATABASE_URL`. Operator status may display a
redacted database URL, but it must not print database passwords or raw
connection secrets.

## Worker Foundation

The local worker foundation declares future capabilities for source-snapshot
ingestion, validation-plan execution, and control-receipt ledger appends. Those
capabilities are advertised as planned, not implemented.

The worker is ready for a later Temporal adapter because it already names the
namespace, task queue, workflow hints, and process identity. It is not a
production worker yet and should not be deployed as one.

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
