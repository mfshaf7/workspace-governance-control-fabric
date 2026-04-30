"""FastAPI health and status surface for the control-fabric scaffold."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI

from control_fabric_core import (
    AUTHORITY_CONTRACT_REF,
    PACKAGE_VERSION,
    RUNTIME_REPO,
    status_snapshot,
)


def create_app(repo_root: str | Path | None = None) -> FastAPI:
    """Create the API app without mutating authority state."""

    app = FastAPI(
        title="Workspace Governance Control Fabric",
        version=PACKAGE_VERSION,
        description=(
            "Local-first runtime surface for governance-control-fabric status. "
            "Authority mutation remains owned by upstream systems."
        ),
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "service": RUNTIME_REPO,
            "version": PACKAGE_VERSION,
        }

    @app.get("/readyz")
    async def readyz() -> dict[str, Any]:
        snapshot = status_snapshot(repo_root)
        return {
            "ready": snapshot["ready"],
            "status": "ready" if snapshot["ready"] else "not-ready",
            "service": RUNTIME_REPO,
            "version": PACKAGE_VERSION,
            "checks": snapshot["required_paths"],
            "authority_contract_ref": AUTHORITY_CONTRACT_REF,
        }

    @app.get("/v1/status")
    async def status() -> dict[str, Any]:
        return status_snapshot(repo_root)

    return app


app = create_app()
