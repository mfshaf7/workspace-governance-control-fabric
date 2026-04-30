# Project Structure

The control fabric is split by runtime responsibility:

- `apps/cli`: operator CLI entrypoint for compact local workflow commands.
- `apps/api`: FastAPI health, readiness, and status surface. Deployment remains
  blocked until platform and security gates approve runtime adoption.
- `apps/worker`: Temporal-ready worker diagnostic entrypoint and future
  background validation execution surface.
- `packages/control_fabric_core`: shared runtime primitives and record helpers.
- `packages/control_fabric_core/db`: SQLAlchemy metadata for fabric-local graph,
  source snapshot, validation plan, validation run, receipt, readiness,
  escalation, and ledger records.
- `schemas`: versioned runtime manifest schemas consumed before graph
  ingestion.
- `examples`: minimal valid runtime manifests used by tests and operator
  documentation.
- `migrations`: Alembic migration history for the fabric-local PostgreSQL
  schema.
- `docs/operations`: primary operator-facing workflow documentation.

The database foundation is intentionally policy-neutral. It stores graph,
receipt, readiness, escalation, and ledger state for the runtime, but it does
not decide which validations are required. Scoped validation planning remains a
separate ART feature so the planner can consume workspace-governance contracts
instead of hardcoding validation policy into storage.

The worker foundation is intentionally execution-neutral. It declares the
future Temporal task-queue boundary and planned worker capabilities, but it does
not import the Temporal SDK, connect to a Temporal server, poll a queue, or run
long-lived workflows. Source snapshots, validation plan execution, and receipt
ledger writes remain planned capabilities until their scoped ART slices land.

The implementation must continue to consume the authority contract from
`workspace-governance/contracts/governance-control-fabric-operator-surface.yaml`
instead of redefining policy locally.

The governance manifest schema is therefore limited to runtime ingestion shape:
repo, component, validator, and projection declarations must cite
`authority_refs` by id. The schema does not decide which policies are required,
which security finding is acceptable, or which platform gate is approved.

The first graph-ingestion primitive converts a valid manifest into in-memory
fabric-local graph records:

- authority-reference nodes
- repo nodes
- component nodes
- validator nodes
- projection nodes
- edges that preserve declared authority, ownership, validation scope, and
  projection source/output relationships

Persistence into the SQLAlchemy graph tables remains a separate implementation
step. This keeps schema ingestion testable without making runtime storage the
authority source.
