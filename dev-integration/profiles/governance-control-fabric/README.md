# Governance Control Fabric Dev-Integration Profile

This is the proposed `dev-integration` profile for the Workspace Governance
Control Fabric runtime.

Current lifecycle in the shared workspace contract:

- `proposed`
- not self-serve launchable

The profile exists now to record the runtime-lane decision before the control
fabric proceeds further into API, worker, storage, graph, receipt, or ledger
implementation. It prevents implementation work from silently assuming a
runtime lane that has not been admitted yet.

## What It Will Run

- control-fabric FastAPI service from this repo
- control-fabric worker from this repo
- local platform-managed PostgreSQL for graph, receipt, and ledger state
- workspace-governance contracts mounted or synced as read-only authority input
- local session artifacts that bind source repos, profile state, and smoke
  evidence

## Current Proposed Boundary

The command scripts intentionally fail closed while this profile remains
`proposed`. They are present so the workspace registry can validate that the
owner repo has a real profile surface, but they must not be treated as an
approved runtime lane.

The profile becomes self-serve only after:

- platform-engineering accepts the runtime fit, storage model, and
  suspend/resume semantics
- security-architecture records any required review for the active runtime
  boundary
- workspace-governance changes the profile lifecycle to `active`
- the scripts are replaced with runnable local-k3s commands

Runtime state model:

- `persistent`

Persistent is selected because the control fabric will hold graph, receipt, and
ledger state during long-running governance work. Shared smoke must remain
read-only. If mutating ledger or receipt smoke is needed later, create a
separate disposable companion profile instead of writing test traffic into this
persistent working lane.

## Operator Actions

When this profile becomes active, it will use the shared platform runner:

- `make devint-up PROFILE=governance-control-fabric`
- `make devint-status PROFILE=governance-control-fabric`
- `make devint-access PROFILE=governance-control-fabric`
- `make devint-smoke PROFILE=governance-control-fabric`
- `make devint-down PROFILE=governance-control-fabric`
- `make devint-reset PROFILE=governance-control-fabric`
- `make devint-promote-check PROFILE=governance-control-fabric`

Until activation, those commands must not be advertised as working runtime
commands.

## Smoke Scope

The future shared smoke path must stay read-only and prove:

- API readiness
- component inventory read
- authority contract load
- validation planner dry run
- receipt and ledger smoke

Smoke must not write to governed stage or prod state. It must not mutate the
persistent working ledger unless a separate disposable companion profile is
approved for that purpose.

## Stage Handoff Checks

The governed `stage` handoff is not ready until it proves:

- API readiness
- component inventory read
- authority contract load
- validation planner dry run
- receipt and ledger smoke

These checks must mirror `stage_handoff.required_checks` in `profile.yaml` and
the workspace registry entry.

## References

- `workspace-governance/contracts/developer-integration-policy.yaml`
- `workspace-governance/contracts/developer-integration-profiles.yaml`
- `workspace-governance/contracts/components.yaml`
- `workspace-governance/docs/work-home-routing-contract.md`
