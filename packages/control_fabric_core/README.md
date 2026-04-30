# Control Fabric Core

`control_fabric_core` owns shared runtime primitives used by the CLI, API, and
worker apps.

Current slice:

- runtime identity constants
- authority-boundary references
- bootstrap status snapshot helpers
- database settings without leaking secrets
- runtime governance manifest schema and dependency-free manifest validation
- deterministic manifest-to-graph ingestion primitives
- read-only manifest graph query helpers for repo, component, validator,
  projection, authority, and ART-oriented scopes
- deterministic validation planning primitives for `smoke`, `scoped`, `full`,
  and `release` tiers
- bounded validation execution helpers that run manifest-planned command checks
  without `shell=True`, write stdout/stderr to local artifacts, and emit compact
  receipts and ledger events
- SQLAlchemy metadata for fabric-local graph, receipt, readiness, escalation,
  and ledger records
- Temporal-shaped worker settings and planned capability metadata without
  runtime connections or long-running workflow behavior

This package must not copy or redefine workspace-governance policy. Policy
meaning stays in the upstream authority contracts.

The governance manifest schema is an ingestion boundary for runtime graph
planning. It records repo, component, validator, and projection declarations
with authority-reference ids so later graph ingestion can prove which upstream
contracts were consumed.

Manifest-to-graph ingestion returns in-memory node and edge records that match
the fabric-local graph model. It does not persist records or mutate upstream
authority stores.

Graph query helpers produce compact slices from those in-memory graph records
for operator surfaces. They are intentionally file-backed in this phase so API
and CLI users can inspect repo, component, and ART-oriented context without a
database dependency or upstream authority write.

Validation planning helpers consume the same manifest and emit compact,
operator-safe plan records. They select only manifest-declared validators,
expand repo-relative changed-file targets through manifest repo and component
scopes, record suppressed validators with reasons, and stop at the decision
layer. They may mark a check as `skip_fresh_receipt` when an input receipt is
successful, fresh, and the validator declares safe reuse. They do not run
commands, create receipts, append ledger events, or approve readiness.

Validation execution helpers consume a `ValidationPlan` and stay inside the
implementation boundary. They run only planned command checks, treat unsupported
check types as blocked, suppress execution when the planner decision is not
`planned`, and produce:

- stdout/stderr artifact references with sha256 digests, byte counts, and line
  counts
- a `ControlReceipt` that omits raw command output
- a `LedgerEvent` suitable for append-only JSONL storage

The execution helpers do not persist to PostgreSQL yet, mutate upstream
authority, decide readiness, or replace Review Packets for ART work.
