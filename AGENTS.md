# Workspace Governance Control Fabric Agent Notes

This repository is the runtime implementation repo for the Workspace Governance
Control Fabric.

Read `README.md` first. Then verify whether the requested change belongs here
or in an upstream authority repo.

Primary operator surface:

- `docs/operations/operator-surface.md`

Authority contract:

- `workspace-governance/contracts/governance-control-fabric-operator-surface.yaml`

## What This Repo Owns

- runtime implementation for the governance control fabric
- governance graph and validation planning code
- admission, readiness, receipt, and ledger runtime surfaces
- policy/projection adapters that execute existing workspace governance rules
- API, worker, and CLI implementation for the control fabric

## What This Repo Does Not Own

- workspace-governance contracts, schemas, routing rules, or maturity doctrine
- platform-engineering deployment approval, environment state, or promotion
  gates
- security-architecture standards, threat posture, or review decisions
- operator-orchestration-service workflow-broker contracts or OpenProject
  adapters
- product-specific runtime implementation

## Working Rules

- Treat `workspace-governance` as the contract and doctrine source of truth.
- Treat `platform-engineering` as the deployment and release authority.
- Treat `security-architecture` as the security review authority.
- Treat `operator-orchestration-service` as the broker-owned workflow adapter
  authority.
- Do not copy upstream policies into this repo as local truth.
- Do not mutate workspace contracts directly from this runtime implementation.
- Do not represent local implementation state as approved deployment state.
- After the initial empty-repo bootstrap, use a branch and pull request for
  meaningful Git-tracked changes.
- If a change affects trust boundaries, identity, secrets, policy enforcement,
  evidence integrity, or AI-enabled action paths, route the security review
  requirement before claiming completion.
- If a change creates or materially changes an operator-facing workflow, add or
  update one primary operator instruction surface in this repo.
- If a change alters CLI, API, record, profile, blocker, escalation, or denied
  action semantics, update the workspace-governance authority contract before
  implementing the changed meaning here.

## Review Guidelines

Treat these as high-risk review areas:

- control-fabric contract drift from `workspace-governance`
- workspace-governance contract dependency changes without matching authority
  updates
- platform deployment boundary confusion with `platform-engineering`
- security review boundary changes without `security-architecture` evidence
- validation coverage gaps in admission, receipt, ledger, or projection logic
- direct writes to authority stores that should remain owned by another repo

Reviewers should verify that implementation behavior stays inside this repo's
runtime boundary and that source-of-truth changes land in the owning repo.
