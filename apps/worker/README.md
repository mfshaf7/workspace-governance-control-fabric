# Control Fabric Worker

This app will own future background execution for validation plans, receipts,
and ledger writes.

Current slice:

- reserve the worker app boundary
- document that long-running worker behavior is intentionally deferred

Temporal-ready worker scaffolding is owned by the later worker skeleton slice.
