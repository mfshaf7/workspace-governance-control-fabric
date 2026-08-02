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
- returns terminal `blocked`, `timed-out`, or `unavailable` results after the
  bounded execution has produced evidence
- makes contract and idempotency violations non-retryable, keeps known
  pre-result availability and timeout failures retryable, and preserves native
  Temporal cancellation only after the isolated owner process has stopped
- heartbeats every two seconds for cancellation delivery, bounds owner
  execution including spawn to four minutes, and bounds TERM grace plus
  process-group exit confirmation to ten additional seconds
- writes owner evidence only under an attempt-specific staging root and grants
  canonical evidence authority by atomic rename only after the complete process
  group is confirmed absent; unconfirmed attempts remain quarantined and cannot
  mutate committed evidence during a Temporal retry
- pre-binds receipt artifact references to the committed root so the atomic
  rename preserves valid custody paths and receipt digests
- shields the bounded stop-and-confirm task from cancellation until that task
  returns, then propagates the cancellation outcome
- replaces exception details with stable public error types and messages
- refuses `wgcf-worker run` until explicit activation and Security review
  evidence are present
- exposes a separate `wgcf-worker controlled-proof status` boundary for one
  Platform-issued, digest-pinned commissioning context
- polls the controlled path only on
  `wgcf.controlled-proof.validation-readiness.v1` as
  `wgcf-controlled-proof-activity-worker`
- rejects expired, replaced, cross-session, wrong-source, wrong-namespace,
  wrong-queue, and wrong-identity requests before owner execution
- stores a separate reference-only WGCF owner receipt without changing the
  normal compact activity result consumed by OOS

Run locally after installing dependencies:

```bash
PYTHONPATH=packages/control_fabric_core/src:apps/worker/src python3 -m wgcf_worker status --repo-root .
```

`wgcf-worker run` is intentionally fail-closed by default. Source readiness is
not runtime activation, governed-stage admission, or production authority.

The commissioning worker is also fail-closed by default:

```bash
PYTHONPATH=packages/control_fabric_core/src:apps/worker/src \
  python3 -m wgcf_worker controlled-proof status --repo-root .
```

`controlled-proof run` requires explicit enablement, execution authorization,
an exact raw-byte owner-context digest, a worker-image source revision that
matches the context, a writable controlled-evidence root, and Temporal address,
namespace, queue, and identity values that match the mounted context. The source
revision is baked into `/opt/wgcf/build/source-revision`; a deployment variable
cannot replace it. The image provisions the default evidence root, but a real
commissioning run must mount durable storage there or set
`WGCF_CONTROLLED_PROOF_EVIDENCE_ROOT` to another durable writable mount. The
worker rechecks the context and image provenance while polling and before
committing a successful receipt. Platform Engineering remains responsible for
generating and mounting the permit-derived context and evidence storage; this
source does not issue a permit or activate the build-admitted Temporal profile.
