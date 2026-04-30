# Project Structure

The control fabric is split by runtime responsibility:

- `apps/cli`: operator CLI entrypoint for compact local workflow commands.
- `apps/api`: FastAPI health, readiness, status, and read-only graph query
  surface. Deployment remains blocked until platform and security gates approve
  runtime adoption.
- `apps/worker`: Temporal-ready worker diagnostic entrypoint and future
  background validation execution surface.
- `packages/control_fabric_core`: shared runtime primitives and record helpers.
- `packages/control_fabric_core/db`: SQLAlchemy metadata for fabric-local graph,
  source snapshot, validation plan, validation run, receipt, readiness,
  escalation, and ledger records.
- `schemas`: versioned runtime manifest, receipt, policy-decision, runtime
  governance record, evidence projection, and ledger event schemas consumed or
  emitted by the local runtime.
- `policies/opa`: OPA/Rego policy surface files for admission, validation
  blocking, and policy-ledger recordability.
- `examples`: minimal valid runtime manifests used by tests and operator
  documentation.
- `migrations`: Alembic migration history for the fabric-local PostgreSQL
  schema.
- `docs/operations`: primary operator-facing workflow documentation.
- `docs/architecture/governance-operations-console-readiness.md`: API
  readiness criteria for the future Governance Operations Console.
- `docs/architecture/context-governance-gateway-integration.md`: integration
  seam for a future Context Governance Gateway packet producer.

The database foundation is intentionally policy-neutral. It stores graph,
receipt, readiness, escalation, and ledger state for the runtime, but it does
not decide which validations are required. Scoped validation planning remains a
separate ART feature so the planner can consume workspace-governance contracts
instead of hardcoding validation policy into storage.

The worker foundation is intentionally queue-neutral. It declares the future
Temporal task-queue boundary and planned worker capabilities, but it does not
import the Temporal SDK, connect to a Temporal server, poll a queue, or run
long-lived workflows. The core library now owns the local validation execution
and receipt/ledger primitives; worker activation remains a later runtime
adapter.

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

The first graph-query primitive remains file-backed and read-only. API and CLI
query surfaces load a repo-local manifest, build the in-memory graph, and return
compact repo, component, validator, projection, authority, or ART-oriented
graph slices. They do not persist records, run validations, approve readiness,
or mutate upstream authority stores.

## Validation Planning Model

The first validation-planning primitive is deterministic and execution-free. It
turns a valid governance manifest plus an operator target into a compact plan
record with:

- a normalized target scope such as `workspace`, `repo:<name>`,
  `component:<id>`, `art:<delivery-id>`, or
  `changed-file:<repo-relative-path>`
- a requested validation tier: `smoke`, `scoped`, `full`, or `release`
- selected manifest-declared validators, preserving command, owner, scopes,
  check type, required posture, receipt-reuse decision, and tier metadata
- suppressed validators with explicit reasons
- a planner decision: `planned`, `no_matching_validators`, or `blocked`

The planner does not execute validators and does not decide policy from local
code. It only selects validators that were already declared in the manifest,
expands changed-file targets through manifest repo and component declarations,
and marks checks as reusable only when a safe-to-reuse validator has a fresh
successful receipt input.

## Validation Execution Model

The first validation-execution primitive consumes a `ValidationPlan` and
produces compact proof records without becoming a policy engine:

- only `planned` planner decisions are executable; blocked or review-required
  plans emit a blocked/operator-review receipt without running commands
- only command checks are executed in this slice
- commands run from the supplied repo root with `shell=False`
- leading environment assignments such as `PYTHONPATH=...` are parsed
  deterministically
- stdout and stderr are written to local artifact files
- receipts contain artifact ids, digests, byte counts, line counts, exit codes,
  durations, planner decision context, and outcome
- receipts do not embed raw stdout/stderr
- ledger events reference receipts and artifacts for append-only audit

The execution model is intentionally local-first. The CLI now composes this
through `wgcf check`, writes compact receipt JSON, appends a local ledger event,
and lists receipt metadata through `wgcf receipts list`. PostgreSQL
persistence, API-side validation execution, CLI `wgcf run --plan`, and worker
queue execution stay in later slices so this layer remains testable and
bounded.

## Policy Admission Model

The first policy-admission primitive is a runtime decision helper, not a policy
authority. It consumes supplied authority refs, receipt refs, optional waiver
metadata, and a repo or component subject. It returns a compact policy decision
with:

- outcome: `allow`, `deny`, `blocked`, `review_required`, or `waived`
- machine-readable reasons and required actions
- authority refs and receipt refs used for the decision
- a stable decision id and timestamp
- optional waiver metadata when a validation issue is explicitly waived

The OPA/Rego files under `policies/opa` are the durable policy-engine surface
for later OPA integration. The Python helper keeps Phase 1 testable without
requiring an OPA binary in local validation. Both surfaces must keep the same
boundary: no upstream workspace policy truth, platform release approval, or
security acceptance is made in this repo.

## Runtime Governance Record Model

Runtime governance records are fabric-local audit records for decisions and
references that operators need to see quickly:

- blocker and impediment decisions preserve decision path, owner, impact, next
  action, authority refs, and evidence refs
- approval and waiver records preserve upstream authority refs, approver or
  waiver metadata, expiry posture, and decision outcome
- risk posture records preserve ROAM-style state and risk ownership without
  accepting risk locally
- change-record events link changed surfaces to compact receipt, artifact, and
  policy-decision refs

Every runtime governance record sets `authority_boundary` to
`record-only-not-authority`. Ledger events make the record auditable, but WGCF
does not mutate ART, approve security posture, accept risk, or replace platform
release gates.

## Evidence Projection Model

The first evidence-projection primitive adapts compact control receipts into
downstream workflow surfaces without copying raw artifacts:

- ART completion evidence receives completion summary, changed surfaces,
  validation evidence, and test evidence rendered from receipt metadata.
- Review Packets receive item evidence refs, changed-surface explanations,
  validation evidence, test evidence, and rollback boundary text linked to the
  same receipt.
- Git and change records receive receipt ids, artifact digests, and policy
  decision ids instead of embedded runtime logs.

Projection records always set `raw_artifacts_embedded` to `false`. This keeps
the control fabric as the runtime evidence authority while ART, Review Packets,
and Git records stay compact, reviewable, and linked back to receipt digests.

## Future Operator Console Readiness

The Governance Operations Console is future scope. The control fabric must first
stabilize API semantics for status, graph, validation, receipts, readiness
decisions, ledger events, and escalations. Console-facing routes must preserve
authority boundaries and compact evidence behavior before any UI is built.

## Future Context Packet Producer Seam

A future Context Governance Gateway may produce governed context packets for AI,
operator, CI, or automation workflows. WGCF should consume packet metadata and
receipt refs only. It must not own raw context capture, redaction scanners,
object storage, or LLM gateway behavior.
