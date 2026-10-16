---
security_evidence:
  review_areas: [delivery, runtime]
  reviewed_artifacts:
    - contracts/prototype-landing/manifest.json
    - contracts/prototype-maturity/manifest.json
  findings: []
  risks: []
  workstreams: [WS-007]
  notes: Consume the existing reviewed Studio Closure registry evolution; retain exact schema/source denial and existing activation gates.
---

# Current Studio registry readiness bundles

## Summary

- Date: 2026-10-16
- Short title: synchronize Landing and Maturity with the current approved Studio registry
- Environment: local dev-integration acceptance and native source tests
- ART: existing blocked acceptance1298, no new repair child
- Landing unit: maintenance-1275-studio-registry-schema-wgcf

## Classification

Accepted-owner contract drift repair. This updates stale consumer copies, not
the canonical record model or a policy exception. The caller/issuer repair is
separate and already operating-proven.

## Ownership

Studio owns its registry schema and existing Closure source evolution. WGCF
owns its pinned consumer copies and readiness implementation. Security accepts
the exact source set; Platform commissions it. OOS and Console retain workflow
and presentation authority.

## Root Cause

The preserved real Console Landing request passed repaired authentication but
returned503. A read-only snapshot in the actual WGCF API reported that Studio's
source schema differed from the reviewed bundle. Landing and Maturity still
copied a4951c88, predating Studio Closure9fdf82e08caf14074f804beaeabdb6569b174ade.
Current Studio and Closure already agree on defdc5c2: delivery-governed and
source_custody. Historical self-consistent fixtures did not prove compatibility
with the current shared registry.

## Source Changes

Refresh both copies and their declared fingerprints/source minimum to that
existing Studio evolution. No runtime code, API, canonical data, activation
flag, caller role, arbitrary latest-schema trust or validation bypass changes.
Native Git cases cover an unrelated graduating Prototype beside Landing and
Maturity targets, source immutability, agreement with Closure and rejection of
an unaccepted old/tampered schema.

## Artifact And Deployment Evidence

Require full committed owner CI-equivalent validation, API/worker image builds,
packaged source verification and actual current-Studio read-only snapshots for
both readers. Security exact-source/index acceptance precedes normal Platform
recommissioning. Historical activation records remain historical; do not fake
new operating evidence in them.

## Live Verification

No live-success claim is made from this source record. The original Proposal1305
Landing request must be continued through the Console after commissioning.
Missing-token authenticated404 checks do not substitute for domain source
readiness. Preserve the capture, repository and request; no hidden app or
packet generation is permitted.

## Follow-Up

Security acceptance/index, ordered activation, both source-reader operating
proofs, same-request Console continuation and exact source-resource retirement.
The known Prototype authoring/packet/handoff gaps remain separate acceptance
obligations; this repair does not close Epic1275.
