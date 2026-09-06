# Workspace Governance Control Fabric

`workspace-governance-control-fabric` is the runtime implementation repo for
the Workspace Governance Control Fabric.

Its purpose is to make workspace governance faster, more observable, and less
dependent on slow Git-only runtime checks while preserving the existing
workspace authority model.

This repo does not replace the policy source of truth. It implements governed
runtime services that consume and enforce truth owned by other repos.

## Role

The control fabric is intended to become the execution layer for workspace
governance operations such as validation planning, admission checks, receipt
generation, ledger/event recording, and fast operator-facing governance views.

It owns implementation for:

- governance graph and dependency resolution runtime
- validation planner and execution receipts
- admission and readiness evaluation services
- evidence, receipt, and ledger runtime surfaces
- policy/projection adapters for operator workflows
- API, worker, and CLI implementation for the control fabric

It must not own:

- canonical workspace contracts, schemas, or maturity rules
- release authority, deployment approval, or environment promotion
- security standards, threat posture, or security review decisions
- Workspace Delivery ART work-state truth
- product-specific runtime implementation

## Ownership Boundaries

The active authority split is:

- `workspace-governance` owns contracts, schemas, workspace-root guidance,
  maturity rules, routing rules, and generated governance artifacts.
- `workspace-governance-control-fabric` owns runtime implementation of the
  control fabric.
- `platform-engineering` owns approved deployment state, version pinning,
  promotion gates, shared runners, and environment adoption.
- `security-architecture` owns trust-boundary standards, review criteria,
  findings, and security posture.
- `operator-orchestration-service` owns broker-backed operator workflow APIs
  and OpenProject workflow adapters.

Cross-repo references should use those repos as authoritative sources instead
of copying their policies here.

## Current State

This repository has an active local and `dev-integration` implementation
foundation. Governed stage and production activation remain separate Platform
and Security decisions.

The primary operator surface is now defined before implementation:

- [docs/operations/operator-surface.md](docs/operations/operator-surface.md)

That surface is constrained by the workspace-owned contract in
`workspace-governance`:

- <https://github.com/mfshaf7/workspace-governance/blob/main/contracts/governance-control-fabric-operator-surface.yaml>

## Project Structure

- `apps/cli/` owns the future `wgcf` operator CLI. The scaffold supports
  `wgcf status`, read-only `wgcf graph query` manifest graph slices, and
  fabric-local lifecycle retention planning and confirmed cleanup through the
  Python entrypoint.
- `apps/api/` owns the FastAPI service boundary. The current implementation
  exposes `GET /healthz`, `GET /readyz`, `GET /v1/status`, `GET /v1/graph`,
  `GET /v1/graph/query`, `POST /v1/validation-plans`,
  `POST /v1/validation-runs`, `GET /v1/receipts`,
  `GET /v1/receipts/{receipt_id}`, `GET /v1/metrics/receipts`,
  `POST /v1/readiness/evaluate`,
  `POST /v1/agent-actions/evaluate`,
  `POST /v1/art/graph`, `POST /v1/art/readiness`, and
  `POST /v1/art/evidence-packet` with compact runtime metadata. The
  dev-integration API also exposes the bounded Delivery ART registry at
  `POST /v1/artifacts/delivery-art`,
  `GET /v1/artifacts/delivery-art/{digest_hex}`, and
  `POST /v1/artifacts/delivery-art/{digest_hex}/reconcile`, plus artifact-bound
  readiness at `POST /v1/readiness/delivery-art` and
  `GET /v1/readiness/delivery-art/{receipt_token}`. Prototype-to-Delivery
  ingress readiness is exposed separately at
  `POST /v1/readiness/prototype-ingress` and
  `GET /v1/readiness/prototype-ingress/{receipt_token}`. Repository admission
  readiness uses `POST /v1/readiness/repositories` and
  `GET /v1/readiness/repositories/{receipt_token}`. Repository custody policy
  readiness is a separate, disabled-by-default boundary at
  `POST /v1/readiness/repository-custody` and
  `GET /v1/readiness/repository-custody/{decision_token}`.
  Repository lifecycle readiness is exposed separately at
  `POST /v1/readiness/repository-lifecycle` and
  `GET /v1/readiness/repository-lifecycle/{decision_token}`.
  Workspace Intake evaluation is available for isolated integration at
  `POST /v1/readiness/workspace-intake` and
  `GET /v1/readiness/workspace-intake/{receipt_token}`; normal runtime
  activation remains denied pending the intake activation gates.
  Active-inventory promotion evaluation uses
  `POST /v1/readiness/workspace-inventory` and
  `GET /v1/readiness/workspace-inventory/{readiness_token}`. It checks one
  admitted entrant against committed intake and inventory truth and returns the
  exact Workspace Governance readiness artifact without changing canonical YAML.
  Existing inventory lifecycle changes use the separate
  `POST /v1/readiness/workspace-inventory-lifecycle` and
  `GET /v1/readiness/workspace-inventory-lifecycle/{readiness_token}` routes.
  They evaluate update, suspend, restore, and retire requests against committed
  inventory plus append-only history without changing either source file.
- `apps/worker/` owns the WGCF Temporal activity adapter. It exposes a
  connection-free status command, registers only the validation/readiness
  activity, heartbeats for cancellation while owner work remains bounded in an
  isolated process group, atomically promotes evidence from per-attempt staging
  only after group-exit confirmation, pre-binds artifact references to their
  committed location so promotion does not invalidate receipt custody, and
  refuses runtime startup until explicit activation gates pass. CI publishes it
  as a separate worker image so validator tooling does not expand the API image
  runtime surface. The same image also exposes a separate, default-denied
  controlled-proof command that is pinned to a distinct identity and queue and
  requires a permit-derived owner context before it can connect.
- `dev-integration/profiles/governance-control-fabric/` owns the local-k3s
  dev-integration lane for the API runtime and PostgreSQL metadata store. It
  deploys the published WGCF API image and local PostgreSQL through the shared
  platform runner, runs database migrations, writes session/access/smoke
  artifacts under the profile state root, and remains separate from governed
  stage or prod deployment approval.
- `packages/control_fabric_core/` owns shared runtime primitives such as
  bootstrap status, authority-boundary references, database settings,
  SQLAlchemy models, runtime governance manifest schema helpers,
  manifest-to-graph ingestion primitives, read-only graph query helpers,
  workspace validator catalog ingestion, deterministic validation planning
  primitives, invocation-class performance budgets, bounded validation execution,
  compact receipts, local ledger event helpers, operator-safe plan/check,
  receipt-list, receipt-inspection, and readiness-decision helpers, broker ART
  runtime-context ingestion, ART readiness
  receipts, bootstrap policy admission decisions, governed agent-action policy
  decisions against a digest-pinned Workspace Governance authority bundle,
  runtime governance records,
  compact evidence projection adapters, and local retention plus ledger
  compaction controls. It also owns strict canonical JSON, content-addressed
  Delivery ART storage, append-only registry metadata, immutable custody
  receipts, reconciliation for approved artifact classes, pinned Delivery ART
  schema validation, four-level readiness evaluation, and immutable readiness
  receipts.
- `contracts/agent-action/` is a digest-pinned runtime snapshot of the
  Workspace Governance agent-action authority, request schema, policy-decision
  schema at the exact #951 source commit, plus WGCF-local evaluator fixtures.
  WGCF validates the authority bundle before issuing a decision; the copied
  files and local fixtures do not become local policy authority.
- `contracts/prototype-ingress/` pins the exact Prototype Delivery packet
  schema emitted by Workspace Prototype Studio and defines WGCF-local request
  and receipt schemas for non-mutating ingress evaluation.
- `contracts/repository-readiness/` defines the non-mutating repository
  readiness request and receipt and pins the exact OOS consumer-reference
  schema. Workspace Governance remains the repository admission authority.
- `contracts/repository-custody/` pins the exact Workspace Governance custody
  and lifecycle authority plus artifact schemas. WGCF uses the bundle only to
  issue immutable readiness decisions; OOS remains workflow authority.
- `contracts/workspace-intake/` pins Workspace Intake v2 authority and defines
  WGCF's evaluation envelope and non-mutating readiness receipt. Committed
  inventory is read from the configured authority checkout, never its dirty
  working files. OOS owns review, merge observation, and terminal readback.
- `contracts/workspace-active-inventory/` pins the promotion contract and v2
  intake, repository, product, and component schemas from Workspace Governance.
  WGCF uses them only for committed-source readiness and immutable readback;
  Workspace Governance remains canonical and OOS remains workflow owner.
- `schemas/governance-manifest.schema.json` defines the versioned runtime
  manifest input schema for repo, component, validator, and projection metadata.
- `schemas/validation-receipt.schema.json` and `schemas/ledger-event.schema.json`
  define the compact proof and append-only event shapes emitted by local
  validation execution. Raw validator output belongs in referenced artifacts,
  not in receipts or ART notes.
- `schemas/controlled-proof-owner-context.schema.json`,
  `schemas/controlled-proof-activity-request.schema.json`, and
  `schemas/controlled-proof-owner-receipt.schema.json` define WGCF's strict
  commissioning input and reference-only receipt boundary. They do not issue a
  permit or activate a runtime.
- `schemas/policy-decision.schema.json` and `policies/opa/` define the first
  policy decision record and OPA/Rego policy surface. Runtime code consumes
  authority refs and receipts; upstream policy truth stays in
  `workspace-governance`.
- `schemas/evidence-projection.schema.json` defines compact projection records
  that adapt control receipts into ART closeout evidence, Review Packet
  evidence, and Git/change-record references without embedding raw artifacts.
- `schemas/art-readiness-receipt.schema.json` and
  `schemas/art-evidence-packet.schema.json` define broker-safe ART readiness
  and evidence packet records. These records help OOS decide when a mutation is
  safe, but OOS remains the ART mutation authority.
- `schemas/runtime-governance-record.schema.json` defines fabric-local
  governance records for blocker, approval, waiver, risk, and change evidence
  events. These records are references and audit state only; they do not grant
  approval authority.
- `examples/governance-manifest.example.json` provides a valid minimal manifest
  that references upstream authority sources instead of copying their policy
  meaning.
- `migrations/` owns Alembic migrations for fabric-local graph, receipt,
  readiness, escalation, and ledger tables. These tables store runtime evidence
  and projections only; they are not upstream authority stores.
- `scripts/validate_project.py` validates the scaffold without requiring
  network access or external services.

See these architecture surfaces for the scaffold boundary and future
integration seams:

- [docs/architecture/project-structure.md](docs/architecture/project-structure.md)
- [docs/architecture/governance-operations-console-readiness.md](docs/architecture/governance-operations-console-readiness.md)
- [docs/architecture/context-governance-gateway-integration.md](docs/architecture/context-governance-gateway-integration.md)

## Operating Model

All meaningful changes should land through a branch and pull request after the
initial empty-repo bootstrap.

Before changing authority boundaries, deployment behavior, security posture, or
operator workflows:

- update the owning source-of-truth repo when policy changes
- keep implementation changes here limited to the runtime behavior this repo
  owns
- route deployment-state changes through `platform-engineering`
- route security decisions through `security-architecture`
- bind accepted delivery work back to the Workspace Delivery ART through
  broker-owned evidence and review packets

## Validation

The initial validation surface checks that the repository keeps its minimum
governance documentation, review controls, and Python scaffold in place.

Local validation:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
.venv/bin/python scripts/validate_project.py --repo-root .
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src:apps/worker/src .venv/bin/python -m unittest discover -s tests
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src .venv/bin/python -m wgcf_cli status --repo-root .
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src .venv/bin/python -m wgcf_cli graph query --repo-root . --scope repo:workspace-governance-control-fabric
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src .venv/bin/python -m wgcf_cli plan --repo-root . --scope repo:workspace-governance-control-fabric --tier smoke
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src .venv/bin/python -m wgcf_cli check --repo-root . --scope repo:workspace-governance-control-fabric --tier smoke
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src .venv/bin/python -m wgcf_cli catalog plan --repo-root . --workspace-root /home/mfshaf7/projects --scope component:workspace-governance --profile local-read-only --tier smoke
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src .venv/bin/python -m wgcf_cli catalog check --repo-root . --workspace-root /home/mfshaf7/projects --scope component:workspace-governance --profile local-read-only --tier smoke
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src .venv/bin/python -m wgcf_cli receipts list --repo-root .
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src .venv/bin/python -m wgcf_cli inspect --repo-root . --receipt <receipt-id-or-path>
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src .venv/bin/python -m wgcf_cli metrics receipts --repo-root .
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src .venv/bin/python -m wgcf_cli readiness --repo-root . --target operator-surface:wgcf-cli --profile local-read-only
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src .venv/bin/python -m wgcf_cli lifecycle plan --repo-root .
PYTHONPATH=packages/control_fabric_core/src:apps/worker/src .venv/bin/python -m wgcf_worker status --repo-root .
PYTHONPATH=packages/control_fabric_core/src:apps/worker/src .venv/bin/python -m wgcf_worker controlled-proof status --repo-root .
```

The scaffold validator also verifies that the static governance manifest schema
matches the runtime schema helper and that the example manifest passes manifest
preflight, graph-ingestion checks, repo/ART-scope graph query checks, and a
scoped validation plan build. It also runs a synthetic local validator through
the bounded execution path to prove receipt generation suppresses raw output
and emits a ledger event.

Validation execution currently lives in the core library. It executes only
manifest-planned command checks, uses `subprocess.run(..., shell=False)`,
supports simple leading environment assignments such as `PYTHONPATH=...`,
writes stdout/stderr to local artifacts, records sha256 digests and byte/line
counts, and returns an operator-safe receipt plus ledger event. If the input
plan is blocked or requires operator review, execution is suppressed and the
receipt outcome records that state instead of claiming success.

The dev-integration profile provisions bounded versioned object storage, an
API workload identity, method-scoped registry callers, and PostgreSQL metadata
for the Delivery ART artifact registry.
Validation-run stdout/stderr is command output and intentionally remains local;
it is not an approved artifact class for that registry. Registry persistence is
limited to the artifact classes accepted by the routed Security review:
architecture packets, work-start records, and Review Packets. OOS production of
those artifacts and safe OpenProject reference projection remain separate work;
the registry does not mutate ART or accept arbitrary evidence.

The pinned architecture contract accepts both schema v1 and v2. Version 1
retains owner-repository merge ordering for compatibility. Version 2 keeps ART
work dependencies, source-backed Landing Unit ordering, and required human
gates as separate structures. Artifact-bound readiness validates their exact
coverage, owner bindings, endpoints, and acyclic ordering against the durable
artifact, so repeated Landing Units from one owner repository remain
unambiguous without treating the owner name as a source-order identity.

The CLI now exposes that flow through `wgcf plan`, `wgcf check`,
`wgcf receipts list`, `wgcf inspect`, and `wgcf readiness`. `wgcf check`
writes raw stdout/stderr to local artifact files, writes a compact receipt JSON
under `.wgcf/receipts` by default, and appends a local ledger JSONL event.
`wgcf inspect` reads only compact receipt JSON under the configured receipt
directory. `wgcf readiness` blocks unknown targets or profiles and appends a
local readiness ledger event. The API exposes the same local-first contract
through `POST /v1/validation-plans`, `POST /v1/validation-runs`,
`GET /v1/receipts`, `GET /v1/receipts/{receipt_id}`, and
`POST /v1/readiness/evaluate`. The worker composes those same primitives behind
the platform-gated `wgcf.validation-readiness.v1` activity queue without
copying raw output into Temporal history. It terminates the isolated owner
process group before acknowledging cancellation or its bounded runtime limit.

`wgcf lifecycle plan` inspects fabric-local `.wgcf` artifacts, receipts, and
ledger state without mutating anything. `wgcf lifecycle apply --confirm`
deletes only planned safe candidates, exports old ledger lines before
compaction, and appends a lifecycle ledger event. The matching API routes are
`POST /v1/lifecycle/retention-plan` and
`POST /v1/lifecycle/retention-apply`.

Receipts and readiness decisions now carry compact correlation ids and metrics.
`wgcf metrics receipts` and `GET /v1/metrics/receipts` summarize receipt counts,
check counts, artifact counts, and outcomes without opening raw artifacts.

Catalog-backed validation is now the normal shadow-parity path for workspace
validator invocation. `wgcf catalog plan` and `wgcf catalog check` load the
workspace-owned `workspace-governance/contracts/governance-validator-catalog.yaml`,
apply the catalog profile, safety class, representative scope, and
`wgcf_invocation` metadata, then generate a runtime manifest and compact
receipt. Broad catalog command families with unresolved placeholders are
suppressed rather than guessed. Profile-gated live/runtime reads require
`--operator-approved` and still cannot mutate ART, platform state, security
posture, or workspace contracts.

Policy admission currently lives in the core library. It evaluates bootstrap
repo/component admission inputs, validation blocking posture, waiver posture,
and policy-decision ledger events from supplied authority refs and receipt refs.
It does not make workspace policy truth or security acceptance decisions.

Evidence projection currently lives in the core library. It converts
receipt-linked runtime evidence into compact downstream views for ART
completion evidence, source-backed Review Packets, and Git/change-record
references. These projections carry receipt ids, digests, policy decision refs,
and artifact refs only; raw runtime output stays in receipt-linked artifacts.

ART readiness projection currently lives in the core library. It consumes
broker-owned continuation, execution-summary, quality, roadmap, PM2 projection,
and projection-state context as read-only input, builds a compact graph, detects
metadata/narrative/projection/stale-parent drift before mutation, blocks last
child completion when the parent Feature is not closeout-ready, and returns
recommendations such as repair narrative, sync projection, stale-open closeout,
or proceed through OOS. It never writes ART directly.

Runtime governance records currently live in the core library. They record
blocker decisions, approval and waiver references, risk posture, and
change-record evidence links as fabric-local records and ledger events. The
records preserve the authority boundary explicitly: WGCF records the decision
or reference, while ART, OOS, security, platform, and workspace-governance
remain the upstream authorities for their domains.

Database migration dry run after dependencies are installed:

```bash
PYTHONPATH=packages/control_fabric_core/src alembic upgrade head --sql
```

The default database URL is local-only and can be overridden with
`WGCF_DATABASE_URL`. Operator status redacts database passwords.

Local API smoke after dependencies are installed:

```bash
uvicorn wgcf_api.app:app --app-dir apps/api/src --host 127.0.0.1 --port 8080
```

Shared dev-integration access after profile activation:

```bash
make -C /home/mfshaf7/projects/platform-engineering devint-up PROFILE=governance-control-fabric
make -C /home/mfshaf7/projects/platform-engineering devint-status PROFILE=governance-control-fabric
make -C /home/mfshaf7/projects/platform-engineering devint-smoke PROFILE=governance-control-fabric
make -C /home/mfshaf7/projects/platform-engineering devint-access PROFILE=governance-control-fabric
make -C /home/mfshaf7/projects/platform-engineering devint-down PROFILE=governance-control-fabric
```

The dev-integration API and PostgreSQL metadata store run in local k3s. The API
is reached through the profile-owned Service and port-forward. It is suitable
for console/API contract iteration, not for stage or production evidence.

Primary upstream sources:

- <https://github.com/mfshaf7/workspace-governance>
- <https://github.com/mfshaf7/platform-engineering>
- <https://github.com/mfshaf7/security-architecture>
- <https://github.com/mfshaf7/operator-orchestration-service>
