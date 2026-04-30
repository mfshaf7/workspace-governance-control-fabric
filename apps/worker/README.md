# Control Fabric Worker

This app will own future background execution for validation plans, receipts,
and ledger writes.

Current slice:

- provide the `wgcf-worker` diagnostic entrypoint
- expose Temporal-shaped namespace, task-queue, and address settings
- declare future worker capabilities without implementing workflow execution
- document that long-running worker behavior is intentionally deferred

Run locally after installing dependencies:

```bash
PYTHONPATH=packages/control_fabric_core/src:apps/worker/src python3 -m wgcf_worker status --repo-root .
```

The worker is Temporal-ready in shape only. It does not import the Temporal SDK,
connect to a Temporal service, poll a task queue, or run production workflow
logic in this slice.
