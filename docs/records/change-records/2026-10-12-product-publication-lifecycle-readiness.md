# Product Publication Lifecycle Readiness

## Summary

- Date: 2026-10-12
- Delivery item: `#1267`
- Landing unit: `delivery-894-lifecycle-readiness`
- Owner: `workspace-governance-control-fabric`
- Runtime posture: non-mutating evaluation on the existing Product Publication route

## Authority

Workspace Governance remains the policy, artifact, Product Inventory, and
append-only inventory-history authority. The evaluator pins merge
`fab38a8266512b0004081039b017904c9c55f288` and the exact Product Publication
contract, owner-manifest schema, and Workspace Inventory history schema from
that revision.

WGCF evaluates committed evidence. It does not create a lifecycle event,
publication receipt, owner manifest, Security decision, access grant, release,
or runtime state.

## Change

- preserve schema-v1 publication evaluation and readiness compatibility;
- add schema-v2 archive and restore evaluation;
- verify the latest committed product lifecycle event, exact digest and
  sequence, predecessor, action, resulting record state, and lifecycle receipt
  identity;
- require an exact retirement event for schema-v2 archive;
- limit restore to `archived` to `unlisted` after an exact owner restore event
  whose predecessor is a suspension or retirement event;
- preserve the prior publication receipt, lifecycle binding, and committed
  history digest in immutable readiness evidence;
- continue to reject stale, contradictory, unauthorized, and replay-conflicting
  evidence before downstream mutation.

The initial lifecycle evaluator source retained the earlier internal-only
authority bundle even though its schema-v2 code targeted the later lifecycle
policy. This maintenance correction binds the bundle to the already-approved
Workspace Governance lifecycle authority and adds an exact regression check so
the source gate cannot again claim lifecycle readiness with the superseded
policy digest.

## Validation

Focused tests cover schema-v1 compatibility, positive archive and restore,
caller-scoped readback, exact replay, conflicting replay, stale history,
contradictory history, fixed restore target, authentication, integrity, and
non-mutation. Repository-wide validation and the full isolated owner suite are
required before review.

## Rollback

Revert this Landing Unit. Existing schema-v1 Product Publication readiness
continues to be the prior boundary; OOS lifecycle-maintenance source must remain
inactive and cannot claim archive or restore readiness.
