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

Only the bootstrap status, manifest graph ingestion, read-only graph query,
validation planning, core-library validation execution, core-library policy
admission, core-library runtime governance records, and core-library evidence
projection surfaces are implemented now. Treat the remaining CLI commands and
API routes below as the minimum required interface contract for later slices,
not as currently available runtime commands.

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

Use this flow once the CLI exists:

1. Check fabric status.
2. Capture a source snapshot from the workspace root.
3. Plan validation for a workspace, repo, component, or operator surface.
4. Run the plan and emit a receipt.
5. Inspect the receipt instead of rereading raw output.
6. Evaluate readiness under a named profile.
7. Review the ledger for handoff or audit.
8. Explain decisions when a result is denied, blocked, or unclear.

The default operator output must be compact. Full validation output belongs in
artifacts referenced by receipts and ledger events.

## Required CLI Shape

```bash
wgcf status
wgcf graph query --scope repo:<name> --manifest <path>
wgcf graph query --scope component:<name> --manifest <path>
wgcf graph query --scope art:<delivery-id> --manifest <path>
wgcf sources snapshot --workspace-root <path>
wgcf plan --scope workspace|repo|component|operator-surface --target <id> --profile <profile>
wgcf run --plan <plan-id-or-file> --emit-receipt
wgcf inspect --receipt <receipt-id-or-path>
wgcf readiness --target workspace|repo:<name>|component:<name>|operator-surface:<id> --profile <profile>
wgcf ledger tail --limit <n>
wgcf explain --decision <decision-id>
```

Required CLI behavior:

- default to compact human-readable summaries
- support `--json` for automation when implemented
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

## Required API Shape

The API is a future integration surface. Do not deploy it until platform and
security gates approve the runtime posture.

Required route meanings:

- `GET /healthz`
- `GET /readyz`
- `GET /v1/status`
- `GET /v1/graph`
- `GET /v1/graph/query?scope=<scope>`
- `POST /v1/source-snapshots`
- `POST /v1/validation-plans`
- `POST /v1/validation-runs`
- `GET /v1/receipts/{receipt_id}`
- `POST /v1/readiness/evaluate`
- `GET /v1/ledger/events`
- `GET /v1/decisions/{decision_id}/explain`

No required route mutates upstream authority stores.

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
plan from manifest-declared validators, and execute a plan through the core
library. The graph and plan records are fabric-local projections only; they are
not persisted by this slice and do not mutate authority stores.

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

Manifest validators may declare `reuse_policy.safe_to_reuse` and
`reuse_policy.freshness_seconds`. When the planner receives matching successful
receipt records that are still fresh, it marks those checks as
`skip_fresh_receipt` candidates in the plan.

The planner decision can be `planned`, `no_matching_validators`, or `blocked`.
It must explain selected checks, suppressed validators, and any operator-review
reason. The core execution primitive must respect that decision. If the
decision is blocked or requires operator review, it emits a compact receipt
without running validators.

Validation execution uses the schemas at:

- `schemas/validation-receipt.schema.json`
- `schemas/ledger-event.schema.json`

Current execution behavior:

- runs only manifest-planned command checks
- runs with `shell=False` from the supplied repo root
- supports simple leading environment assignments such as `PYTHONPATH=...`
- writes full stdout/stderr to local artifact files
- includes only artifact refs, digests, byte counts, line counts, exit codes,
  duration, planner decision, and outcome in receipts
- appends ledger events as JSONL through the core helper
- never embeds raw stdout/stderr in receipts or ART notes

CLI `wgcf run`, API `POST /v1/validation-runs`, database persistence, and worker
queue execution remain later slices.

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
  - future local-k3s runtime integration after platform and security gates
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
