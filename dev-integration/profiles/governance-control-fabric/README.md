# Governance Control Fabric Dev-Integration Profile

This is the `dev-integration` profile for the Workspace Governance Control
Fabric runtime.

Runtime boundary:

- local-k3s Deployment and Service managed by the shared platform runner
- local-k3s PostgreSQL StatefulSet and Service for fabric-local metadata
- persistent local state root under `.dev-integration/governance-control-fabric/<operator>`
- read-only smoke for API, graph, validation-plan, and receipt metadata reads
- no stage or prod deployment approval

This profile is the first real runtime-access path for the control fabric. It
uses the published WGCF API image in local k3s so downstream console/API
contract work can consume a system service today while stage/prod deployment
gates remain separate.

## What It Will Run

- control-fabric FastAPI service from the published WGCF image
- local k3s Service for operator and future console access
- local PostgreSQL for graph, receipt, readiness, and ledger state
- workspace-governance contracts mounted or synced as read-only authority input
- local session artifacts that bind source repos, profile state, and smoke
  evidence

## Runtime Boundary

Runtime state model:

- `persistent`

Persistent is selected because the control fabric will hold session, graph,
receipt, and ledger state during long-running governance work. Shared smoke
must remain read-only. If mutating ledger or receipt smoke is needed later,
create a separate disposable companion profile instead of writing test traffic
into this persistent working lane.

The current profile starts PostgreSQL as a local k3s StatefulSet, runs database
migrations from the WGCF image, starts the API as a local k3s Deployment,
exposes it through a ClusterIP Service, and writes the operator access details
to the profile state root. It does not create a stage deployment and does not
activate the worker runtime.

## Operator Actions

Use the shared platform runner:

- `make devint-up PROFILE=governance-control-fabric`
- `make devint-status PROFILE=governance-control-fabric`
- `make devint-access PROFILE=governance-control-fabric`
- `make devint-smoke PROFILE=governance-control-fabric`
- `make devint-down PROFILE=governance-control-fabric`
- `make devint-reset PROFILE=governance-control-fabric`
- `make devint-promote-check PROFILE=governance-control-fabric`

## Smoke Scope

The shared smoke path stays read-only and proves:

- API readiness
- component inventory read
- authority contract load
- database migration
- validation planner dry run
- receipt and ledger metadata read

Smoke must not write to governed stage or prod state. It must not mutate the
persistent working ledger unless a separate disposable companion profile is
approved for that purpose.

## Stage Handoff Checks

The governed `stage` handoff is not ready until it proves:

- API readiness
- component inventory read
- authority contract load
- database migration
- validation planner dry run
- receipt and ledger metadata read

These checks must mirror `stage_handoff.required_checks` in `profile.yaml` and
the workspace registry entry.

## References

- `workspace-governance/contracts/developer-integration-policy.yaml`
- `workspace-governance/contracts/developer-integration-profiles.yaml`
- `workspace-governance/contracts/components.yaml`
- `workspace-governance/docs/work-home-routing-contract.md`
