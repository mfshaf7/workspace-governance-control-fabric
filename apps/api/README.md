# Control Fabric API

This app owns the FastAPI service surface for the Workspace Governance Control
Fabric runtime.

Current slice:

- implement `GET /healthz`
- implement `GET /readyz`
- implement `GET /v1/status` with version and authority-reference metadata
- implement `GET /v1/graph` for a read-only manifest graph projection
- implement `GET /v1/graph/query?scope=<scope>` for compact repo, component,
  validator, projection, authority, or ART-oriented graph slices
- implement `GET /v1/source-snapshots/status` for compact source snapshot
  status without raw authority content or full digest maps
- implement `POST /v1/validation-plans` for compact validation-plan records
- implement `POST /v1/validation-runs` for bounded local validation execution
  with compact receipt and ledger output
- implement `GET /v1/receipts` for compact local receipt metadata
- implement `GET /v1/receipts/{receipt_id}` for compact receipt inspection
  without reopening raw artifacts
- implement `POST /v1/readiness/evaluate` for local readiness decisions with a
  fabric-local ledger event
- implement `POST /v1/art/graph` for compact broker-owned ART context graph
  projection
- implement `POST /v1/art/readiness` for pre-mutation ART readiness receipts
  and OOS route recommendations
- implement `POST /v1/art/evidence-packet` for completion-preflight-safe ART
  and Review Packet evidence projected from WGCF receipts

The API can run bounded local validation checks through the same core-library
safety controls used by the CLI. It writes raw stdout/stderr only to
receipt-linked local artifacts, emits compact receipts and ledger events, and
does not mutate upstream authority stores.

The ART routes are read/projection routes. They do not mutate OpenProject and
do not replace `operator-orchestration-service` as the ART write authority.

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
