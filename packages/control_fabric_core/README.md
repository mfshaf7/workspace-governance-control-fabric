# Control Fabric Core

`control_fabric_core` owns shared runtime primitives used by the CLI, API, and
worker apps.

Current slice:

- runtime identity constants
- authority-boundary references
- bootstrap status snapshot helpers
- database settings without leaking secrets
- digest-only source snapshot ingestion across workspace authority repos, repo
  manifests, component interface manifests, and dev-integration profiles
- runtime governance manifest schema and dependency-free manifest validation
- deterministic manifest-to-graph ingestion primitives
- idempotent PostgreSQL/SQLAlchemy persistence helpers for source snapshots,
  authority refs, graph nodes, graph edges, source digests, and freshness
  markers
- read-only manifest graph query helpers for repo, component, validator,
  projection, authority, and ART-oriented scopes
- deterministic validation planning primitives for `smoke`, `scoped`, `full`,
  and `release` tiers
- bounded validation execution helpers that run manifest-planned command checks
  without `shell=True`, write stdout/stderr to local artifacts, and emit compact
  receipts and ledger events
- operator-surface helpers that build validation plans, run bounded local
  checks, write compact receipts, append ledger events, and list receipt
  metadata for CLI/API surfaces
- operator-surface helpers that inspect compact receipt records and evaluate
  known local readiness targets without reading raw artifacts or mutating
  upstream authority
- bootstrap policy admission helpers that evaluate repo/component admission,
  validation blocking, waiver posture, and receipt-linked policy ledger events
- runtime governance record helpers for blocker decisions, approvals, waivers,
  risk posture, and change evidence links
- compact evidence projection helpers that adapt control receipts into ART,
  Review Packet, and Git/change-record evidence references without copying raw
  artifacts
- ART runtime-context helpers that ingest broker continuation context, quality
  and projection state, detect pre-mutation drift, and produce readiness
  receipts and OOS-safe recommendations without mutating ART
- SQLAlchemy metadata for fabric-local graph, receipt, readiness, escalation,
  and ledger records
- Temporal-shaped worker settings and planned capability metadata without
  runtime connections or long-running workflow behavior

This package must not copy or redefine workspace-governance policy. Policy
meaning stays in the upstream authority contracts.

Source snapshot ingestion records file digests, local Git refs, source kinds,
and missing-source exclusions for the authority surfaces WGCF depends on. It
does not copy contract contents into the fabric and does not write to upstream
repos.

The governance manifest schema is an ingestion boundary for runtime graph
planning. It records repo, component, validator, and projection declarations
with authority-reference ids so later graph ingestion can prove which upstream
contracts were consumed.

Manifest-to-graph ingestion returns in-memory node and edge records that match
the fabric-local graph model. It does not persist records or mutate upstream
authority stores.

Graph persistence helpers write source snapshots, authority-reference digests,
freshness markers, graph nodes, graph edges, and synthetic scope nodes to the
fabric-local SQLAlchemy model. They are idempotent by primary key, flush without
committing, and leave transaction ownership with the caller.

Graph query helpers produce compact slices from those in-memory graph records
for operator surfaces. They are intentionally file-backed in this phase so API
and CLI users can inspect repo, component, and ART-oriented context without a
database dependency or upstream authority write.

Validation planning helpers consume the same manifest and emit compact,
operator-safe plan records. They select only manifest-declared validators,
expand repo-relative changed-file targets through manifest repo and component
scopes, infer dev-integration profile scopes from profile paths, honor
manifest-declared impact scopes such as ART and release targets, record
suppressed validators with reasons, and stop at the decision layer. They may
mark a check as `skip_fresh_receipt` when an input receipt is successful,
fresh, still matches authority-ref digests when invalidation is enabled, and
the validator declares safe reuse. Plans also carry explicit cache, timeout,
retry, output-budget, and WGCF invocation-class decisions. They do not run
commands, create receipts, append ledger events, or approve readiness.
Every plan also returns explicit `check_statuses` so downstream operators can
separate selected, suppressed, blocked, waived, stale, failed, and
external-owner-required checks without scraping reason text.

Performance-budget helpers classify WGCF runtime paths before they become
synchronous operator gates. Routine continuation and graph reads are
`inline-fast`, draft submit is `receipt-check`, completion/blocker/risk
readiness is `hard-gate`, projection and full quality work are
`checkpoint-batch`, and unknown future operations are `offline-advisory` until
classified. Validation execution applies these budgets as hard caps over
timeout, retry, and output limits while still writing raw output only to
receipt-linked artifacts.

Validation execution helpers consume a `ValidationPlan` and stay inside the
implementation boundary. They run only planned command checks, treat unsupported
check types as blocked, suppress execution when the planner decision is not
`planned`, and produce:

- a bootstrap self-validation contract that keeps `scripts/validate_project.py`
  as the direct scaffold authority and prevents WGCF receipts from becoming
  the proof that WGCF itself is bootstrapped
- validator safety preflight for safety class, profile, executable allowlist,
  allowed roots, sanitized environment handling, explicit env allow/block
  policy, and output limits
- stdout/stderr artifact references with sha256 digests, byte counts, and line
  counts
- compact custody summaries that bind receipt and ledger records back to the
  same artifact ids and digest manifest without embedding raw output
- compact timeout, retry, and output-budget metadata
- compact performance-budget metadata, including effective timeout, retry, and
  output caps
- a `ControlReceipt` that omits raw command output
- a `LedgerEvent` suitable for append-only JSONL storage

The execution helpers do not persist to PostgreSQL yet, mutate upstream
authority, decide readiness, or replace Review Packets for ART work.

Operator-surface helpers compose planning and execution into the local
operator workflow. They write receipts under a caller-supplied receipt
directory, append a JSONL ledger event, and return compact metadata for CLI/API
rendering. Receipt-list and receipt-inspection views read only receipt JSON
metadata and do not open raw stdout/stderr artifacts. Readiness helpers block
unknown targets or profiles and append a readiness ledger event while keeping
the decision fabric-local.

Policy admission helpers consume authority refs and receipt refs supplied by
the runtime caller. They return compact allow, deny, blocked, waived, or
review-required decisions and can build `policy.decision.recorded` ledger
events linked to receipts. OPA/Rego files under `policies/opa` define the
policy-engine surface, but upstream policy meaning still belongs to
`workspace-governance`.

Evidence projection helpers consume `ControlReceipt` records and optional
policy decisions to produce downstream-safe records for three surfaces:

- ART completion evidence payload fields
- Review Packet item-evidence, validation, test, and rollback references
- Git/change-record references to receipt ids, artifact digests, and policy
  decision ids

Projection helpers do not read raw artifact files, embed stdout/stderr, mutate
ART, or write change records. They only convert receipt metadata into compact
operator-facing references.

ART readiness helpers consume broker-owned context as read-only input. They
build compact ART graph records from continuation, planning, execution-summary,
quality, roadmap, PM2 projection, and projection-state payloads; detect missing
metadata, weak Feature narratives, stale-open parent candidates, dirty
projection state, and milestone parent drift; then return readiness receipts
with deterministic routes such as `work-item.update`, `projection.sync`, or
`work-item.stale-open-close`. They can also generate completion-preflight-safe
ART evidence packets from WGCF receipts. OOS remains the only ART mutation
authority.

Runtime governance record helpers create fabric-local records for blocker
decisions, approval and waiver references, risk posture, and change-record
evidence links. They can emit ledger events such as
`governance.blocker.recorded` or `governance.change.recorded`, but they remain
record-only. They do not approve work, accept risk, mutate ART, or replace the
upstream authority that made the decision.
