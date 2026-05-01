# Governance Operations Console API Readiness

## Purpose

Define what WGCF must expose before a future Governance Operations Console can
be built.

The console is intended to become an operator-facing view over governance
runtime state. It is not an authority source and it must not bypass the owner
repos, platform release gates, security review, or broker-owned ART mutation
paths.

## Current State

WGCF currently exposes local-first CLI and API surfaces for:

- status
- readiness health
- manifest graph projection
- scoped graph query
- validation plan creation
- bounded local validation run execution
- receipt metadata listing
- receipt inspection by id
- lifecycle retention planning and confirmed cleanup
- local readiness decision evaluation
- broker-owned ART graph projection
- ART readiness receipt evaluation
- ART evidence packet projection from WGCF receipts

These surfaces are enough for local operator workflow and source validation.
They are not enough for a deployed console yet.

## Console Readiness Criteria

Before UI implementation starts, WGCF must provide stable, documented API
semantics for these console views:

| Console view | WGCF-owned readiness criteria | Authority boundary |
| --- | --- | --- |
| Runtime status | API reports version, repo root, bootstrap readiness, database posture, module availability, and safe redaction of connection settings. | Platform deployment state remains owned by `platform-engineering`. |
| Governance graph | API returns compact nodes, edges, scopes, authority refs, and projection refs without dumping source contracts. | Contract truth remains owned by `workspace-governance`. |
| Validation plans | API explains selected, suppressed, blocked, and reusable checks for a target and tier. | Required validation policy remains authority-backed, not hardcoded in UI. |
| Receipts | API lists receipt ids, artifact refs, digests, byte and line counts, outcomes, and timestamps without raw stdout or stderr. | Full artifacts remain in the configured artifact store or local artifact path. |
| Lifecycle retention | API returns dry-run cleanup plans, requires explicit confirmation for local cleanup, and exports ledger lines before compaction. | Retention mutates only fabric-local files; upstream authority records remain owned by their source systems. |
| ART readiness | API returns compact broker-context graph summaries, pre-mutation findings, and OOS routes such as narrative repair, projection sync, stale-open closeout, or proceed via broker. | OOS remains the ART mutation authority. WGCF recommendations are not writes. |
| ART evidence packets | API returns completion-preflight-compatible ART evidence fields and Review Packet refs from WGCF receipt metadata. | Raw artifacts remain receipt-linked; Review Packets and ART stay downstream references. |
| Readiness decisions | API returns allow, deny, blocked, review-required, or waived decisions with machine-readable reasons and required actions. | Security and platform approval remain upstream authority decisions. |
| Ledger events | API returns append-only event metadata and linked refs for audit navigation. | Ledger events are runtime audit records, not source-of-truth approval records. |
| Escalations | API returns blocker or escalation records with owner, impact, route, and required next action. | ART blockers route through `operator-orchestration-service`; security deltas route through `security-architecture`. |

## API Behavior Requirements

Console-facing API behavior must:

- default to compact payloads
- expose raw artifact content only through an explicitly approved artifact
  custody path
- preserve stable ids for receipts, decisions, ledger events, artifacts, and
  authority refs
- include `authority_boundary` or equivalent fields when a record is not an
  approval source
- distinguish `unknown`, `blocked`, `denied`, `stale`, `failed`, `waived`, and
  `allowed` states
- include enough failure context for an operator to route the next action
  without reading implementation internals
- support pagination or bounded listing before any central deployment
- keep central/deployed API-side validation execution gated by platform and
  security approval; local-first dev-integration execution remains bounded by
  WGCF safety controls

## Identity And Authorization Expectations

The console must not launch with anonymous or shared-secret-only access.

Minimum future requirements:

- caller identity propagated to WGCF
- role-aware read permissions for operator, auditor, CI, and automation callers
- separate write permissions for any future runtime mutation surface
- audit record for console-driven decisions or exported evidence
- no UI-side elevation around platform, security, workspace-governance, or ART
  authority boundaries

## Non-Goals For The Current Slice

This readiness contract does not:

- build the console UI
- choose the final frontend framework
- expose a live service
- create a new approval authority
- replace OpenProject, Review Packets, or broker-owned ART paths
- approve central or governed-stage API-side validation execution

## First Console-Compatible API Expansion

The first future expansion should add read-only route coverage for:

- ledger event listing
- persistence-backed ART readiness history
- escalation record listing
- decision explanation

Only after those read paths are stable should the console add operator actions.
