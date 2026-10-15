# Repository Provisioning Readiness

## Summary

- Date: 2026-08-29
- Short title: Evaluate repository provisioning controls
- Environment: WGCF source and sandbox runtime only
- Severity: High

## Classification

- Type: policy-readiness implementation
- User-facing impact: OOS can obtain an immutable `create-provider` decision
  for an exact approved GitHub organization target and baseline settings.
  Normal runtime activation remains disabled.

## Ownership

- Owning repo or layer: `workspace-governance-control-fabric`
- Related repos: `workspace-governance`, `operator-orchestration-service`,
  `security-architecture`, `platform-engineering`
- Related architecture: Workspace Delivery ART Epic `#888`, child `#1055`

## Boundary

- Workspace Governance owns provisioning policy and artifact schemas.
- WGCF validates and persists readiness decisions only.
- OOS owns the later provider command, reconciliation, readback, and terminal
  receipt.
- Security and Platform own GitHub App write acceptance and runtime delivery.

## Source Changes

- Synchronized the exact Workspace Governance provisioning authority and
  schemas from merged child `#1054`.
- Added action-specific readiness for `provision-new` while preserving
  `link-existing` behavior.
- Bound allowed provisioning decisions to the exact organization, repository
  name, visibility, initialization, feature toggles, and merge policy.
- Preserved one request identity, one immutable decision, exact replay, and
  fail-closed handling for changed payloads and tampered ledger records.

## Evidence

- Positive sandbox cases issue, replay, and read one `create-provider`
  decision without provider mutation.
- Negative cases reject personal scope, incomplete settings, unsupported
  providers, stale policy, secret-bearing references, and conflicting replay.
- The normal runtime builder remains blocked by the upstream activation flag.

## Residual Work

- OOS child `#1046` must consume the exact decision and implement idempotent
  provider execution and readback.
- Security child `#1047` and Platform child `#1048` must approve and deliver
  the organization-scoped GitHub App authority before runtime activation.

## 2026-10-16 Bounded Source-Activation Maintenance

Landing Unit `delivery-1275-custody-readiness-activation` is
`child_isolated_landing_unit`: the WGCF source, review and rollback boundary is
independent of OOS and Platform. This is operator-approved acceptance-exposed
maintenance under Epic #1275 / #1298, tracked in the existing operating-truth
improvement candidate, not a new ART child.

The real Console request found that the configured runtime factory still
required the upstream authority flag, whose canonical schema fixes it at
`false`. OOS had separately reviewed source activation, but WGCF did not.
Security PR #269 explicitly extends the existing gate to bounded WGCF source
activation before implementation. The manifest pins that merged review at
`ecb9fb383526aaec0dee5c58e0fb1520e30bac29` and its exact content digest.

Reuse WGCF's existing manifest activation/review model for only `link-existing`
and `provision-new`. Validate review metadata and reject widened capability
sets; require the existing profile and Platform enablement as well. Missing or
invalid contract/activation evidence reports bounded runtime unavailability.
Canonical Workspace Governance policy/schema bytes, lifecycle activation,
caller authorization, credentials, provider effects and downstream admission
are unchanged. The upstream policy stays contract-only rather than being
rewritten to make a local runtime start.

Validation covers the actual configured factory with the normal reviewed
bundle and unchanged upstream authority, default/profile/configuration denial,
missing/disabled activation, malformed review and extra-capability denial, and
unchanged lifecycle denial. Image proof must construct that same factory with
normal installed contracts and exact image revision, not only load schemas.
These proofs do not substitute for real Console provider readback and receipts.

Merge order: the source-activation Security review is already merged; land this
WGCF change, refresh the existing composed Security decision for its exact
merged head, then activate through Platform and resume the real Console
journey. No Platform source-pin change or new activation mechanism is needed.
Rollback disables Platform's custody gate or reverts the WGCF source
activation, retaining decisions and custody/provider history. Do not remove
repositories or fabricate a successful workflow receipt.
