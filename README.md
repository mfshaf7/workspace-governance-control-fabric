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

This repository is in governance bootstrap state with the first Python project
scaffold in place.

The first governed slice registers the repo with workspace governance before
runtime implementation begins. Product code, service scaffolding, deployment
state, and security-specific controls should land through later scoped work
items after the repo boundary is admitted.

The primary operator surface is now defined before implementation:

- [docs/operations/operator-surface.md](docs/operations/operator-surface.md)

That surface is constrained by the workspace-owned contract in
`workspace-governance`:

- <https://github.com/mfshaf7/workspace-governance/blob/main/contracts/governance-control-fabric-operator-surface.yaml>

## Project Structure

- `apps/cli/` owns the future `wgcf` operator CLI. The scaffold supports
  `wgcf status` and read-only `wgcf graph query` manifest graph slices through
  the Python entrypoint.
- `apps/api/` owns the FastAPI service boundary. The current implementation
  exposes `GET /healthz`, `GET /readyz`, `GET /v1/status`, `GET /v1/graph`,
  `GET /v1/graph/query`, `POST /v1/validation-plans`,
  `POST /v1/validation-runs`, `GET /v1/receipts`,
  `GET /v1/receipts/{receipt_id}`, `POST /v1/readiness/evaluate`,
  `POST /v1/art/graph`, `POST /v1/art/readiness`, and
  `POST /v1/art/evidence-packet` with compact runtime metadata.
- `apps/worker/` owns the Temporal-ready worker package boundary. The current
  implementation exposes `wgcf-worker status`, declares future worker
  capabilities, and intentionally does not run long-lived workflow behavior.
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
  primitives, bounded validation execution,
  compact receipts, local ledger event helpers, operator-safe plan/check,
  receipt-list, receipt-inspection, and readiness-decision helpers, broker ART
  runtime-context ingestion, ART readiness
  receipts, bootstrap policy admission decisions, runtime governance records,
  and compact evidence projection adapters.
- `schemas/governance-manifest.schema.json` defines the versioned runtime
  manifest input schema for repo, component, validator, and projection metadata.
- `schemas/validation-receipt.schema.json` and `schemas/ledger-event.schema.json`
  define the compact proof and append-only event shapes emitted by local
  validation execution. Raw validator output belongs in referenced artifacts,
  not in receipts or ART notes.
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
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src .venv/bin/python -m wgcf_cli readiness --repo-root . --target operator-surface:wgcf-cli --profile local-read-only
PYTHONPATH=packages/control_fabric_core/src:apps/worker/src .venv/bin/python -m wgcf_worker status --repo-root .
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

The CLI now exposes that flow through `wgcf plan`, `wgcf check`,
`wgcf receipts list`, `wgcf inspect`, and `wgcf readiness`. `wgcf check`
writes raw stdout/stderr to local artifact files, writes a compact receipt JSON
under `.wgcf/receipts` by default, and appends a local ledger JSONL event.
`wgcf inspect` reads only compact receipt JSON under the configured receipt
directory. `wgcf readiness` blocks unknown targets or profiles and appends a
local readiness ledger event. The API exposes the same local-first contract
through `POST /v1/validation-plans`, `POST /v1/validation-runs`,
`GET /v1/receipts`, `GET /v1/receipts/{receipt_id}`, and
`POST /v1/readiness/evaluate`; the worker queue execution path remains a later
platform-gated slice.

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
metadata/narrative/projection/stale-parent drift before mutation, and returns
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
