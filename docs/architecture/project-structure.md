# Project Structure

The control fabric is split by runtime responsibility:

- `apps/cli`: operator CLI entrypoint for compact local workflow commands.
- `apps/api`: FastAPI health, readiness, and status surface. Deployment remains
  blocked until platform and security gates approve runtime adoption.
- `apps/worker`: future background validation execution surface.
- `packages/control_fabric_core`: shared runtime primitives and record helpers.
- `packages/control_fabric_core/db`: SQLAlchemy metadata for fabric-local graph,
  source snapshot, validation plan, validation run, receipt, readiness,
  escalation, and ledger records.
- `migrations`: Alembic migration history for the fabric-local PostgreSQL
  schema.
- `docs/operations`: primary operator-facing workflow documentation.

The database foundation is intentionally policy-neutral. It stores graph,
receipt, readiness, escalation, and ledger state for the runtime, but it does
not decide which validations are required. Scoped validation planning remains a
separate ART feature so the planner can consume workspace-governance contracts
instead of hardcoding validation policy into storage.

The implementation must continue to consume the authority contract from
`workspace-governance/contracts/governance-control-fabric-operator-surface.yaml`
instead of redefining policy locally.
