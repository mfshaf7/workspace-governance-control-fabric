# Repository Readiness v2 Authority Compatibility

## Summary

- Date: 2026-10-04
- Short title: Validate repository readiness against canonical `repos.yaml` v2
- Environment: WGCF source control and local `dev-integration`
- Severity: High

## Classification

- Type: control-fabric contract compatibility repair
- Tracking: `workspace-governance/reviews/improvement-candidates/2026-10-04-work-session-merge-order-recurrence.yaml`
- User-facing impact: Restores the fail-closed repository-readiness decision
  required before the Governance Operations Console can link an Owner Repo
  Catalog value.

## Ownership

- Owning repo or layer: `workspace-governance-control-fabric`
- Related repos: `workspace-governance`, `operator-orchestration-service`,
  `platform-engineering`, `governance-operations-console`
- Delivery consumer: Workspace Delivery ART items `1211` and `1231`

## Root Cause

- Immediate failure: the live readiness endpoint returned
  `authority-contract-invalid` for the exact current Workspace Governance
  authority digest.
- Actual root cause: the evaluator hard-coded `schema_version == 1`, and its
  unit fixture generated only the legacy v1 shape. The canonical
  `contracts/repos.yaml` migrated to schema v2 on 2026-09-06 and now requires a
  versioned record envelope and posture fields.
- Escape: repository-readiness tests did not validate the evaluator against a
  pinned canonical authority schema or the current workspace authority.
- Boundary: Workspace Governance continues to own repository truth. WGCF only
  validates the read-only authority and issues a readiness decision.

## Source Changes

- Pin the canonical Workspace Governance `repos.yaml` v2 schema path, source
  commit, and digest in the repository-readiness manifest.
- Validate the complete parsed authority against that schema before repository
  evaluation.
- Replace the synthetic v1 unit authority with a schema-valid v2 record and add
  a negative test proving v1 now fails closed.
- Keep malformed, duplicate-key, stale-digest, missing-repository, retired, and
  rule/security mismatch outcomes distinct.

## Validation

- `python3 -m unittest tests.test_repository_readiness`
- `python3 scripts/validate_project.py --repo-root .`
- `python3 scripts/run_delivery_art_evidence.py --repo-root .`
- `git diff --check <base>...<head>` after commit
- Local exact-authority proof against
  `workspace-governance/contracts/repos.yaml` digest
  `sha256:94aeb6a58b5bcea176b629546df1577971c94c69516c7b8be544e676cc9fc342`
  returns `ready` with a reference for `openclaw-runtime-distribution`.

## Runtime and Security Posture

- The change adds no mutation authority, identity, secret, network route, or
  deployment privilege.
- Invalid or unrecognized authority shapes continue to fail closed.
- Runtime adoption remains a separate Platform action using the exact merged
  WGCF revision, followed by the existing item `1231` live-backend proof.

## Follow-Up

- Merge this repair before updating the Platform composition source pin.
- Rebuild the existing `refinement-catalog` composition with the merged WGCF
  revision and rerun repository Catalog commissioning.
- Close the improvement candidate only after a separately approved durable
  cross-repo acceptance control prevents synthetic fixtures from replacing the
  canonical authority proof.
