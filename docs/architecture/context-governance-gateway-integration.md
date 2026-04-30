# Context Governance Gateway Integration Seam

## Purpose

Define how a future `context-governance-gateway` can feed governed context
packets into WGCF without making WGCF a context gateway, scanner, artifact
store, or LLM gateway.

The context gateway owns context admission processing:

```text
raw context -> capture -> normalize -> classify -> redact -> slice -> budget -> project -> audit -> packet
```

WGCF owns control-fabric runtime state around the packet:

```text
packet metadata -> authority refs -> readiness/admission checks -> receipts -> ledger -> operator views
```

## Boundary

`context-governance-gateway` owns:

- command, file, CI, terminal, and repo-output capture
- context normalization and classification
- redaction and secret detection integration
- slicing, budgeting, and model-safe packet projection
- raw artifact custody and packet audit trail
- packet receipts and producer-side ledger events

WGCF owns:

- producer registration through authority-backed manifests
- packet metadata ingestion shape
- readiness and admission checks that reference packet receipts
- compact downstream evidence projections
- ledger events that record WGCF-side decisions
- operator-facing views over packet refs and decision refs

WGCF must not store raw operational context by default.

## Minimum Packet Metadata

The integration seam requires model-safe packet metadata shaped enough to audit
and route decisions without embedding raw artifacts:

| Field | Purpose |
| --- | --- |
| `packet_id` | Stable packet identity from the context gateway. |
| `producer_id` | Registered producer identity, such as `context-governance-gateway`. |
| `source_ref` | Command, file, CI run, repo, ART item, or workflow source reference. |
| `captured_at` | Timestamp for freshness and ordering. |
| `artifact_digest` | Digest of the preserved raw artifact or artifact bundle. |
| `redacted_digest` | Digest of the redacted artifact or packet source. |
| `classification` | Deterministic context class and sensitivity posture. |
| `redaction_summary` | Counts and kinds of redactions, not raw secret values. |
| `budget_summary` | Token or output budget requested, used, and suppressed. |
| `policy_profile` | Context profile used, such as `casual`, `developer`, or `enterprise`. |
| `retention_ref` | Retention and artifact custody reference. |
| `receipt_ref` | Producer-side receipt or audit record reference. |
| `safe_excerpt_ref` | Optional reference to the model-safe packet content. |

## Admission Rules

WGCF-side packet admission must fail closed when:

- producer identity is unknown
- authority refs are missing
- packet metadata omits digests
- redaction status is uncertain
- policy profile is missing or incompatible with the target workflow
- retention posture is unknown for enterprise-required artifacts
- packet age exceeds freshness policy
- security review marks the producer or profile suspended

Admission may return `review_required` instead of `deny` when an operator or
security authority must decide the next action.

## Evidence Projection

WGCF may project packet-linked evidence into downstream workflows only as
compact references:

- ART completion evidence can cite packet ids, receipt refs, digests, and
  policy decisions.
- Review Packets can cite packet evidence refs and changed-surface summaries.
- Git or change records can cite packet receipts and artifact digests.

WGCF must not copy raw context, secret-like values, or full terminal output into
ART notes, Review Packets, Git records, or console views.

## Runtime Integration Phases

1. Document the seam and non-goals.
2. Add manifest support for registered context-packet producers.
3. Add schema support for packet metadata and packet receipt refs.
4. Add read-only API and CLI inspection of packet refs.
5. Add admission evaluation for packet metadata.
6. Add platform deployment and security gates before central packet ingestion.

## Non-Goals

This seam does not:

- implement `context-governance-gateway`
- build custom scanners
- replace Presidio, Gitleaks, TruffleHog, OPA, MinIO, S3, LiteLLM, OpenClaw,
  or Ollama
- make WGCF an LLM gateway
- allow raw context projection by default
- bypass security review for AI-adjacent packet flow
