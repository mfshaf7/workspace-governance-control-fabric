# Control Fabric API

This app owns the FastAPI service surface for the Workspace Governance Control
Fabric runtime.

Current slice:

- implement `GET /healthz`
- implement `GET /readyz`
- implement `GET /v1/status` with version and authority-reference metadata
- implement `GET /v1/graph` for a read-only manifest graph projection
- implement `GET /v1/graph/query?scope=<scope>` for compact repo, component,
  validator, projection, authority, or ART-oriented graph slices with budgeted
  pagination
- implement `GET /v1/budgets` for operator-visible invocation-class budgets
  and recommended action shape
- implement `POST /v1/lifecycle/retention-plan` and
  `POST /v1/lifecycle/retention-apply` for confirmed fabric-local artifact,
  receipt, and ledger lifecycle controls
- implement `GET /v1/source-snapshots/status` for compact source snapshot
  status without raw authority content or full digest maps
- implement `POST /v1/validation-plans` for compact validation-plan records
- implement `POST /v1/validation-runs` for bounded local validation execution
  with compact receipt and ledger output
- implement `GET /v1/receipts` for compact local receipt metadata
- implement `GET /v1/receipts/{receipt_id}` for compact receipt inspection
  without reopening raw artifacts
- implement `GET /v1/metrics/receipts` for compact receipt metrics and
  correlation visibility
- implement `POST /v1/readiness/evaluate` for local readiness decisions with a
  fabric-local ledger event
- implement `POST /v1/agent-actions/evaluate` for a bounded `allow`, `deny`, or
  `review-required` decision against exact operator, caller, agent, workflow,
  target, source-version, context, delegation, approval, and replay bindings
- implement `POST /v1/art/graph` for compact broker-owned ART context graph
  projection
- implement `POST /v1/art/readiness` for pre-mutation ART readiness receipts
  and OOS route recommendations
- implement `POST /v1/art/evidence-packet` for completion-preflight-safe ART
  and Review Packet evidence projected from WGCF receipts
- implement `POST /v1/artifacts/delivery-art` for bounded, canonical,
  registry-first persistence of approved Delivery ART artifacts
- implement `GET /v1/artifacts/delivery-art/{digest_hex}` for exact-version
  retrieval with storage, registry, receipt, and lineage verification
- implement `POST /v1/artifacts/delivery-art/{digest_hex}/reconcile` for a
  reference-only consistency receipt
- implement `POST /v1/readiness/delivery-art` for artifact-bound architecture,
  implementation, merge, and operating readiness evaluation
- implement `GET /v1/readiness/delivery-art/{receipt_token}` for immutable
  readiness receipt retrieval
- implement `POST /v1/readiness/prototype-ingress` for authenticated,
  non-mutating evaluation of one exact Prototype Delivery packet
- implement `GET /v1/readiness/prototype-ingress/{receipt_token}` for immutable
  Prototype ingress readiness receipt retrieval
- implement `POST /v1/readiness/repositories` for authenticated repository
  admission-readiness evaluation against exact Workspace Governance authority
- implement `GET /v1/readiness/repositories/{receipt_token}` for immutable
  repository-readiness receipt retrieval
- implement `POST /v1/readiness/repository-custody` for authenticated,
  non-mutating evaluation of one exact repository custody request
- implement `GET /v1/readiness/repository-custody/{decision_token}` for
  immutable custody-readiness decision retrieval

The API can run bounded local validation checks through the same core-library
safety controls used by the CLI. It writes raw stdout/stderr only to
receipt-linked local artifacts, emits compact receipts and ledger events, and
does not mutate upstream authority stores.

The ART routes are read/projection routes. They do not mutate OpenProject and
do not replace `operator-orchestration-service` as the ART write authority.

The agent-action route is an authenticated policy-evaluation boundary. Its
request body contains `request` and `current` objects, is limited to 64 KiB,
and is accepted only from the existing OOS caller scope. The API replaces the
supplied current caller id with the authenticated workload id before
evaluation, validates the digest-pinned Workspace Governance contract, and
appends a compact decision event. It never invokes the owner workflow, embeds
raw context or credentials, or treats agent identity as authorization.

The Delivery ART registry routes are enabled only in `dev-integration`. They
use separate method-scoped caller credentials: OOS may register and read, while
the WGCF reconciler may read and reconcile. Registration accepts only an
`artifact_content` object plus its lowercase canonical `content_digest` and
rejects unsupported classes, duplicate JSON keys, floating-point values,
generated custody fields, stale supersession, and oversized requests. Responses
contain opaque WGCF and Platform receipt references, never object-store
endpoint, bucket, key, version, or credentials. There is no delete route.

Delivery ART readiness is separate from the broker-context
`POST /v1/art/readiness` pre-mutation check. OOS may issue and read readiness
receipts; the WGCF reconciler may read them but cannot issue them. Evaluation
resolves exact durable artifact and dependency refs from the registry, verifies
the pinned Workspace Governance schemas, and emits an immutable,
content-addressed receipt for `architecture-ready`, `implementation-ready`,
`merge-ready`, or `operating-ready`. Operating readiness accepts the bounded
OOS pre-finalization candidate because the readiness receipt must exist before
the finalized Review Packet is persisted. The service does not author an ART
artifact, mutate OpenProject, approve security posture, or activate stage or
production runtime.

Prototype ingress readiness is a separate `dev-integration` boundary. OOS may
submit the exact packet emitted and committed by Workspace Prototype Studio;
the WGCF reconciler may read the resulting receipt but cannot issue it. WGCF
verifies packet integrity, baseline approval, source commit ancestry and tree,
the committed packet record, current Prototype projection, and resolved
repository custody. The immutable `allow` or `deny` receipt explicitly carries
`mutation_authority: none`; OOS remains responsible for any later Delivery
target application.

Repository admission-readiness is also an authenticated `dev-integration`
boundary. OOS may evaluate; the WGCF reconciler may only read. The authority
repository is mounted read-only. A `ready` receipt projects the exact OOS
reference, while missing, retired, stale, and contract-mismatch outcomes remain
non-applicable evidence. The service cannot create repositories or mutate
Catalog state.

Repository custody readiness is separate from admission readiness. Its route
is present, but the normal runtime builder fails closed until both
`WGCF_REPOSITORY_CUSTODY_READINESS_ENABLED=true` and an upstream authority with
`runtime_activation.enabled: true` exist. Injected sandbox services prove the
contract before activation. They evaluate existing-repository linking and the
first organization-scoped GitHub provisioning controls. An allowed
provisioning decision contains the exact approved settings and directs OOS to
`create-provider`; it does not call GitHub. Only OOS may issue a decision; the
WGCF reconciler may read one.

Future Governance Operations Console readiness criteria are documented at:

- [../../docs/architecture/governance-operations-console-readiness.md](../../docs/architecture/governance-operations-console-readiness.md)

The console must consume stable API semantics and compact refs. It must not
invent private UI-only authority, expose raw artifacts, or bypass upstream
platform, security, workspace-governance, or ART ownership boundaries.

Run locally after installing dependencies:

```bash
uvicorn wgcf_api.app:app --app-dir apps/api/src --host 127.0.0.1 --port 8080
```

This service is local-first only until platform and security gates approve a
runtime deployment posture.
