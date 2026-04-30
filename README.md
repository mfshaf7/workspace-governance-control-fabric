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
  and `GET /v1/graph/query` with version and manifest graph metadata.
- `apps/worker/` owns the Temporal-ready worker package boundary. The current
  implementation exposes `wgcf-worker status`, declares future worker
  capabilities, and intentionally does not run long-lived workflow behavior.
- `dev-integration/profiles/governance-control-fabric/` records the proposed
  local-k3s runtime lane. It is not self-serve launchable until platform
  acceptance and required security review move the profile to `active`.
- `packages/control_fabric_core/` owns shared runtime primitives such as
  bootstrap status, authority-boundary references, database settings,
  SQLAlchemy models, runtime governance manifest schema helpers,
  manifest-to-graph ingestion primitives, read-only graph query helpers,
  deterministic validation planning primitives, and future record helpers.
- `schemas/governance-manifest.schema.json` defines the versioned runtime
  manifest input schema for repo, component, validator, and projection metadata.
- `examples/governance-manifest.example.json` provides a valid minimal manifest
  that references upstream authority sources instead of copying their policy
  meaning.
- `migrations/` owns Alembic migrations for fabric-local graph, receipt,
  readiness, escalation, and ledger tables. These tables store runtime evidence
  and projections only; they are not upstream authority stores.
- `scripts/validate_project.py` validates the scaffold without requiring
  network access or external services.

See [docs/architecture/project-structure.md](docs/architecture/project-structure.md)
for the scaffold boundary.

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
python3 -m pip install -e ".[test]"
python3 scripts/validate_project.py --repo-root .
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src python3 -m unittest discover -s tests
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src python3 -m wgcf_cli status --repo-root .
PYTHONPATH=packages/control_fabric_core/src:apps/api/src:apps/cli/src python3 -m wgcf_cli graph query --repo-root . --scope repo:workspace-governance-control-fabric
PYTHONPATH=packages/control_fabric_core/src:apps/worker/src python3 -m wgcf_worker status --repo-root .
```

The scaffold validator also verifies that the static governance manifest schema
matches the runtime schema helper and that the example manifest passes manifest
preflight, graph-ingestion checks, repo/ART-scope graph query checks, and a
scoped validation plan build.

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

Primary upstream sources:

- <https://github.com/mfshaf7/workspace-governance>
- <https://github.com/mfshaf7/platform-engineering>
- <https://github.com/mfshaf7/security-architecture>
- <https://github.com/mfshaf7/operator-orchestration-service>
