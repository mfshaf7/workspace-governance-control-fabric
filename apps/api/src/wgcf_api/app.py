"""FastAPI health, status, and graph query surface for the control fabric."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from control_fabric_core import (
    AUTHORITY_CONTRACT_REF,
    DEFAULT_RECEIPT_DIR,
    PACKAGE_VERSION,
    RUNTIME_REPO,
    build_operator_validation_plan,
    build_graph_from_manifest_file,
    build_source_snapshot,
    graph_summary,
    list_control_receipts,
    query_manifest_file,
    source_snapshot_status,
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

    @app.get("/v1/source-snapshots/status")
    async def source_snapshots_status(
        actor: str = Query("wgcf-api", description="Operator or automation actor to record on the snapshot."),
        workspace_root: str | None = Query(
            None,
            description="Workspace root to snapshot. Defaults to the parent of the WGCF repo root.",
        ),
    ) -> dict[str, Any]:
        try:
            resolved_workspace_root = _resolve_workspace_root(resolved_repo_root, workspace_root)
            snapshot = build_source_snapshot(resolved_workspace_root, actor=actor)
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "source_snapshot": source_snapshot_status(snapshot),
        }

    @app.post("/v1/validation-plans")
    async def validation_plans(request: dict[str, Any]) -> dict[str, Any]:
        scope = str(request.get("scope") or "").strip()
        if not scope:
            raise HTTPException(status_code=400, detail="scope is required")
        tier = str(request.get("tier") or "scoped").strip()
        manifest_path = str(request.get("manifest_path") or DEFAULT_MANIFEST_PATH).strip()
        try:
            manifest_file = _resolve_manifest_path(resolved_repo_root, manifest_path)
            plan = build_operator_validation_plan(manifest_file, scope, tier=tier)
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "manifest_path": str(manifest_file.relative_to(resolved_repo_root)),
            "plan": plan.to_record(),
        }

    @app.get("/v1/receipts")
    async def receipts(
        receipt_dir: str = Query(DEFAULT_RECEIPT_DIR, description="Repo-local compact receipt directory."),
    ) -> dict[str, Any]:
        try:
            receipt_path = _resolve_local_path(resolved_repo_root, receipt_dir, "receipt_dir")
            summaries = [receipt.to_record() for receipt in list_control_receipts(receipt_path)]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "count": len(summaries),
            "receipt_dir": str(receipt_path.relative_to(resolved_repo_root)),
            "receipts": summaries,
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


def _resolve_local_path(repo_root: Path, value: str, label: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(repo_root):
        raise ValueError(f"{label} must stay inside the repository root")
    return resolved


def _resolve_workspace_root(repo_root: Path, workspace_root: str | None) -> Path:
    if workspace_root is None:
        return repo_root.parent
    candidate = Path(workspace_root)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    allowed_root = repo_root.parent.resolve()
    if not resolved.is_relative_to(allowed_root):
        raise ValueError("workspace_root must stay inside the repository parent workspace")
    return resolved


app = create_app()
