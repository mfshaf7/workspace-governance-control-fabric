# Product Publication Migration Constraint Names

## Summary

The first merged-source Product Publication runtime reconciliation exposed a
PostgreSQL migration failure in revision `0015_product_publication`.

## Classification

- owner-repo maintenance linked to Platform activation work item `#1258`
- no new ART child
- runtime impact limited to the pending local `dev-integration` migration

## Root Cause

Two composite unique constraints began with `actor` and omitted explicit
names. SQLAlchemy's naming convention produced long names that PostgreSQL
truncated to the same 63-character identifier, so table creation failed with
`DuplicateTable` before the migration could commit.

## Source Changes

- assign distinct stable names to both composite constraints;
- require every migration-level `UniqueConstraint` to declare an explicit
  name; and
- test that the complete migration set satisfies the rule.

## Validation

The migration contract tests, project validator, Product Publication suite,
and full repository test suite must pass. Platform then rebuilds the exact
WGCF source and proves the migration against PostgreSQL through the normal
`refinement-catalog` reconciliation.

## Rollback

Revert this source change only before successful migration. After migration,
normal capability rollback disables Product Publication runtime without
dropping the evidence table or deleting readiness records.
