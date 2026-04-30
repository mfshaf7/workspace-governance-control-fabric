"""FastAPI health, status, and graph query surface for the control fabric."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from control_fabric_core import (
    AUTHORITY_CONTRACT_REF,
    PACKAGE_VERSION,
    RUNTIME_REPO,
    build_graph_from_manifest_file,
    graph_summary,
    query_manifest_file,
    status_snapshot,
)


DEFAULT_MANIFEST_PATH = "examples/governance-manifest.example.json"


def create_app(repo_root: str | Path | None = None) -> FastAPI:
    """Create the API app without mutating authority state."""

    resolved_repo_root = Path(repo_root or ".").resolve()
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

    @app.get("/v1/graph")
    async def graph(
        manifest_path: str = Query(DEFAULT_MANIFEST_PATH, description="Repo-local governance manifest path."),
    ) -> dict[str, Any]:
        try:
            manifest_file = _resolve_manifest_path(resolved_repo_root, manifest_path)
            graph_projection = build_graph_from_manifest_file(manifest_file)
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "graph": graph_projection.to_records(),
            "manifest_path": str(manifest_file.relative_to(resolved_repo_root)),
            "summary": graph_summary(graph_projection),
        }

    @app.get("/v1/graph/query")
    async def graph_query(
        scope: str = Query(..., description="Graph query scope such as repo:<id>, component:<id>, or art:<id>."),
        manifest_path: str = Query(DEFAULT_MANIFEST_PATH, description="Repo-local governance manifest path."),
    ) -> dict[str, Any]:
        try:
            manifest_file = _resolve_manifest_path(resolved_repo_root, manifest_path)
            result = query_manifest_file(manifest_file, scope)
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "manifest_path": str(manifest_file.relative_to(resolved_repo_root)),
            "query": result.to_record(),
        }

    return app


def _resolve_manifest_path(repo_root: Path, manifest_path: str) -> Path:
    candidate = Path(manifest_path)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(repo_root):
        raise ValueError("manifest_path must stay inside the repository root")
    return resolved


app = create_app()
