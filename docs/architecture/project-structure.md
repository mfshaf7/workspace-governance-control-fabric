# Project Structure

The control fabric is split by runtime responsibility:

- `apps/cli`: operator CLI entrypoint for compact local workflow commands.
- `apps/api`: FastAPI health, readiness, and status surface. Deployment remains
  blocked until platform and security gates approve runtime adoption.
- `apps/worker`: future background validation execution surface.
- `packages/control_fabric_core`: shared runtime primitives and record helpers.
- `docs/operations`: primary operator-facing workflow documentation.

The scaffold deliberately keeps database and worker behavior out of the first
source slices. Those capabilities are separate ART children so their contracts,
tests, and review evidence stay bounded.

The implementation must continue to consume the authority contract from
`workspace-governance/contracts/governance-control-fabric-operator-surface.yaml`
instead of redefining policy locally.
