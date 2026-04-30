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
- implement `GET /v1/receipts` for compact local receipt metadata

The API does not run validators yet. Validation execution remains local CLI and
core-library behavior until platform and security gates approve the runtime
execution posture.

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
