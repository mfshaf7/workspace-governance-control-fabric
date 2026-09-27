# Governance History Projection

## Summary

- Date: 2026-09-28
- ART item: `#1198`
- Landing Unit: `delivery-900-wgcf-governance-history`
- Environment: source and local validation only

## Purpose

Expose one bounded, read-only WGCF projection that the Governance Operations
Console can use to navigate readiness, escalation, ledger, and receipt history.

## Source Changes

- Added authenticated list and detail routes under `/v1/governance-history`.
- Added stable reversible record ids and opaque keyset pagination.
- Normalized existing WGCF persistence records without adding a database or a
  second authority store.
- Added source availability, truncation, freshness, authority-boundary, owner
  next-action, and allowlisted evidence-route metadata.
- Added a public response schema and positive and negative contract tests.

## Trust Boundary

The route has a dedicated read-only Console caller credential. It never returns
stored receipt, readiness, or decision payloads; raw artifacts, local paths,
credentials, and secret-bearing references remain outside the projection.
Unavailable sources are explicit and make the aggregate projection partial.

## Activation

This change does not activate a deployed Console path. Console integration,
Security acceptance, and Platform activation remain owned by ART `#1199`,
`#1200`, and `#1201` respectively.

## Rollback

Remove the governance-history routes, service, schema, and caller configuration
together. Existing WGCF persistence and readiness writers are unchanged.
