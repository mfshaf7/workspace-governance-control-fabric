# Control Fabric API

This app owns the FastAPI service surface for the Workspace Governance Control
Fabric runtime.

Current slice:

- implement `GET /healthz`
- implement `GET /readyz`
- implement `GET /v1/status` with version and authority-reference metadata

Run locally after installing dependencies:

```bash
uvicorn wgcf_api.app:app --app-dir apps/api/src --host 127.0.0.1 --port 8080
```

This service is local-first only until platform and security gates approve a
runtime deployment posture.
