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

This repository is in governance bootstrap state.

The first governed slice registers the repo with workspace governance before
runtime implementation begins. Product code, service scaffolding, deployment
state, and security-specific controls should land through later scoped work
items after the repo boundary is admitted.

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
governance documentation and review controls in place. Runtime validators will
be added when the control-fabric implementation is scaffolded.

Primary upstream sources:

- <https://github.com/mfshaf7/workspace-governance>
- <https://github.com/mfshaf7/platform-engineering>
- <https://github.com/mfshaf7/security-architecture>
- <https://github.com/mfshaf7/operator-orchestration-service>
