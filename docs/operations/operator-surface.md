# Workspace Governance Control Fabric Operator Surface

## Purpose

This is the primary operator instruction surface for the future
Workspace Governance Control Fabric runtime.

The authoritative workflow contract lives in:

- <https://github.com/mfshaf7/workspace-governance/blob/main/contracts/governance-control-fabric-operator-surface.yaml>

This repo implements that contract. It does not redefine workspace policy,
deployment authority, security acceptance, or ART mutation behavior.

## Current State

The repo is still in bootstrap state. The operator surface is intentionally
defined before implementation so the first runtime code does not invent a
different workflow.

Until implementation lands, treat the commands and API routes below as the
minimum required interface contract, not as currently available runtime
commands.

## Authority Boundaries

- `workspace-governance` owns contracts, schemas, workspace-root guidance,
  routing rules, maturity rules, and generated governance artifacts.
- `workspace-governance-control-fabric` owns runtime implementation for
  validation planning, readiness evaluation, receipts, ledger events, API,
  worker, and CLI.
- `platform-engineering` owns deployment state, release gates, version pinning,
  promotion, and runtime adoption.
- `security-architecture` owns security standards, findings, review criteria,
  and security acceptance posture.
- `operator-orchestration-service` owns broker-backed operator workflows,
  OpenProject adapters, ART writes, blockers, Review Packets, and completion
  evidence transport.

The fabric may create local/runtime snapshots, plans, receipts, decisions, and
ledger events. It must route authority mutation to the owning system.

## Operator Flow

Use this flow once the CLI exists:

1. Check fabric status.
2. Capture a source snapshot from the workspace root.
3. Plan validation for a workspace, repo, component, or operator surface.
4. Run the plan and emit a receipt.
5. Inspect the receipt instead of rereading raw output.
6. Evaluate readiness under a named profile.
7. Review the ledger for handoff or audit.
8. Explain decisions when a result is denied, blocked, or unclear.

The default operator output must be compact. Full validation output belongs in
artifacts referenced by receipts and ledger events.

## Required CLI Shape

```bash
wgcf status
wgcf sources snapshot --workspace-root <path>
wgcf plan --scope workspace|repo|component|operator-surface --target <id> --profile <profile>
wgcf run --plan <plan-id-or-file> --emit-receipt
wgcf inspect --receipt <receipt-id-or-path>
wgcf readiness --target workspace|repo:<name>|component:<name>|operator-surface:<id> --profile <profile>
wgcf ledger tail --limit <n>
wgcf explain --decision <decision-id>
```

Required CLI behavior:

- default to compact human-readable summaries
- support `--json` for automation when implemented
- return receipt, artifact, and ledger references for full evidence
- deny or block readiness when authority truth is unknown or stale
- avoid printing raw validation dumps unless explicitly requested

## Required API Shape

The API is a future integration surface. Do not deploy it until platform and
security gates approve the runtime posture.

Required route meanings:

- `GET /healthz`
- `GET /readyz`
- `GET /v1/status`
- `POST /v1/source-snapshots`
- `POST /v1/validation-plans`
- `POST /v1/validation-runs`
- `GET /v1/receipts/{receipt_id}`
- `POST /v1/readiness/evaluate`
- `GET /v1/ledger/events`
- `GET /v1/decisions/{decision_id}/explain`

No required route mutates upstream authority stores.

## Records Operators Should Expect

- `source-snapshot`
- `validation-plan`
- `validation-run`
- `control-receipt`
- `readiness-decision`
- `ledger-event`
- `authority-reference`
- `escalation-record`

Use receipts for operator-safe proof. Use ledger events for audit and handoff.
Use upstream PRs, ART records, platform records, and security reviews for their
own authority domains.

## Database Foundation

The local runtime database stores only fabric-local implementation records:

- governance graph nodes and edges
- source snapshots
- validation plans and runs
- control receipts
- readiness decisions
- ledger events
- escalation records

The database does not become the source of truth for workspace contracts,
platform deployment state, security acceptance, or Delivery ART state. Those
remain owned by their upstream repos and systems. The fabric stores digests,
references, receipts, and decisions derived from those authorities.

Database configuration uses `WGCF_DATABASE_URL`. Operator status may display a
redacted database URL, but it must not print database passwords or raw
connection secrets.

## Profiles

- `local-read-only`
  - local snapshots, validation plans, checks, receipts, and ledger events
- `dev-integration`
  - future local-k3s runtime integration after platform and security gates
- `governed-stage`
  - future governed deployment posture after release and security approval

Unknown authority, stale source snapshots, failed shadow parity, missing owner
boundaries, required security deltas, and platform release gates must not pass
best-effort readiness.

## Blockers And Escalation

The fabric emits an escalation record when it cannot continue honestly.

Required blocker triggers:

- `unknown-authority-source`
- `stale-source-snapshot`
- `shadow-parity-failed`
- `missing-owner-boundary`
- `security-delta-required`
- `platform-release-gate-required`

Routing:

- Active ART impact routes through `operator-orchestration-service`.
- Missing workspace authority routes through `workspace-governance`.
- Security deltas route through `security-architecture`.
- Deployment, version, promotion, and runtime adoption gates route through
  `platform-engineering`.

## Denied Behavior

The fabric must not:

- mutate `workspace-governance` contracts directly
- mutate platform approved deployment state
- make security acceptance decisions
- mutate Delivery ART directly
- execute autonomous AI governance decisions
- hide raw validation output only in chat
- replace Review Packets for source-backed ART work
- treat compact output as full evidence

## Day-One Implementation Rule

The first implementation should be local-first and practical, but it must
preserve the workflow shape:

- source snapshot before validation plan
- validation plan before validation run
- receipt before operator success claim
- ledger event for meaningful fabric actions
- escalation record for blockers instead of silent pass/fail ambiguity

If implementation needs a new command, route, record, profile, or authority
meaning, update the `workspace-governance` contract first.
