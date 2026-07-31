# Control Fabric Worker

This app owns the WGCF side of bounded background validation/readiness work.
Operator Orchestration Service remains the aggregate workflow owner.

Current source:

- provides `wgcf-worker status` without opening a Temporal connection
- registers only `wgcf.validation-readiness.evaluate`
- polls only `wgcf.validation-readiness.v1`
- rejects unknown or authority-expanding payload fields
- stores raw output and local paths only in WGCF-owned evidence storage
- deduplicates completed execution by idempotency key
- refuses `wgcf-worker run` until explicit activation and Security review
  evidence are present

Run locally after installing dependencies:

```bash
PYTHONPATH=packages/control_fabric_core/src:apps/worker/src python3 -m wgcf_worker status --repo-root .
```

`wgcf-worker run` is intentionally fail-closed by default. Source readiness is
not runtime activation, governed-stage admission, or production authority.
