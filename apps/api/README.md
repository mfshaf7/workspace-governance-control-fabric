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
- implement `POST /v1/validation-plans` for compact validation-plan records
- implement `GET /v1/receipts` for compact local receipt metadata

The API does not run validators yet. Validation execution remains local CLI and
core-library behavior until platform and security gates approve the runtime
execution posture.

Run locally after installing dependencies:

```bash
uvicorn wgcf_api.app:app --app-dir apps/api/src --host 127.0.0.1 --port 8080
```

This service is local-first only until platform and security gates approve a
runtime deployment posture.
