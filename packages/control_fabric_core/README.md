# Control Fabric Core

`control_fabric_core` owns shared runtime primitives used by the CLI, API, and
worker apps.

Current slice:

- runtime identity constants
- authority-boundary references
- bootstrap status snapshot helpers
- database settings without leaking secrets
- SQLAlchemy metadata for fabric-local graph, receipt, readiness, escalation,
  and ledger records
- Temporal-shaped worker settings and planned capability metadata without
  runtime connections or long-running workflow behavior

This package must not copy or redefine workspace-governance policy. Policy
meaning stays in the upstream authority contracts.
