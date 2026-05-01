"""FastAPI health, status, and graph query surface for the control fabric."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from control_fabric_core import (
    AUTHORITY_CONTRACT_REF,
    DEFAULT_ARTIFACT_ROOT,
    DEFAULT_LEDGER_EXPORT_DIR,
    DEFAULT_LEDGER_PATH,
    DEFAULT_RECEIPT_DIR,
    DEFAULT_RETENTION_PROFILE,
    PACKAGE_VERSION,
    RUNTIME_REPO,
    apply_retention_plan,
    build_art_runtime_graph,
    build_operator_validation_plan,
    build_graph_from_manifest_file,
    build_source_snapshot,
    build_retention_plan,
    evaluate_operation_budget,
    evaluate_art_readiness,
    graph_summary,
    inspect_control_receipt,
    list_control_receipts,
    operation_budget_records,
    project_receipts_to_art_evidence_packet,
    query_manifest_file,
    run_operator_readiness_evaluation,
    run_operator_validation_check,
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
        budget_profile: str = Query("developer", description="Performance budget profile to apply."),
        limit: int | None = Query(None, description="Maximum nodes and edges to return."),
        offset: int = Query(0, description="Result offset for budgeted pagination."),
        scope: str = Query(..., description="Graph query scope such as repo:<id>, component:<id>, or art:<id>."),
        manifest_path: str = Query(DEFAULT_MANIFEST_PATH, description="Repo-local governance manifest path."),
    ) -> dict[str, Any]:
        try:
            manifest_file = _resolve_manifest_path(resolved_repo_root, manifest_path)
            result = query_manifest_file(
                manifest_file,
                scope,
                budget_profile=budget_profile,
                limit=limit,
                offset=offset,
            )
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "manifest_path": str(manifest_file.relative_to(resolved_repo_root)),
            "query": result.to_record(),
        }

    @app.get("/v1/budgets")
    async def budgets(
        operation: str | None = Query(None, description="Optional operation id to inspect."),
        profile: str = Query("developer", description="Performance budget profile to apply."),
    ) -> dict[str, Any]:
        operations = [operation] if operation else None
        return {
            "budgets": list(operation_budget_records(operations, profile=profile)),
            "evaluation": (
                evaluate_operation_budget(operation, profile=profile).to_record()
                if operation
                else None
            ),
            "profile": profile,
        }

    @app.post("/v1/lifecycle/retention-plan")
    async def lifecycle_retention_plan(request: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = build_retention_plan(
                artifact_root=_resolve_local_path(
                    resolved_repo_root,
                    str(request.get("artifact_root") or DEFAULT_ARTIFACT_ROOT),
                    "artifact_root",
                ),
                export_dir=_resolve_local_path(
                    resolved_repo_root,
                    str(request.get("export_dir") or DEFAULT_LEDGER_EXPORT_DIR),
                    "export_dir",
                ),
                ledger_path=_resolve_local_path(
                    resolved_repo_root,
                    str(request.get("ledger") or DEFAULT_LEDGER_PATH),
                    "ledger",
                ),
                profile=str(request.get("profile") or DEFAULT_RETENTION_PROFILE),
                receipt_dir=_resolve_local_path(
                    resolved_repo_root,
                    str(request.get("receipt_dir") or DEFAULT_RECEIPT_DIR),
                    "receipt_dir",
                ),
                repo_root=resolved_repo_root,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "retention_plan": plan.to_record(),
        }

    @app.post("/v1/lifecycle/retention-apply")
    async def lifecycle_retention_apply(request: dict[str, Any]) -> dict[str, Any]:
        try:
            result = apply_retention_plan(
                actor=str(request.get("actor") or "wgcf-api").strip(),
                artifact_root=_resolve_local_path(
                    resolved_repo_root,
                    str(request.get("artifact_root") or DEFAULT_ARTIFACT_ROOT),
                    "artifact_root",
                ),
                confirm=bool(request.get("confirm", False)),
                export_dir=_resolve_local_path(
                    resolved_repo_root,
                    str(request.get("export_dir") or DEFAULT_LEDGER_EXPORT_DIR),
                    "export_dir",
                ),
                ledger_path=_resolve_local_path(
                    resolved_repo_root,
                    str(request.get("ledger") or DEFAULT_LEDGER_PATH),
                    "ledger",
                ),
                profile=str(request.get("profile") or DEFAULT_RETENTION_PROFILE),
                receipt_dir=_resolve_local_path(
                    resolved_repo_root,
                    str(request.get("receipt_dir") or DEFAULT_RECEIPT_DIR),
                    "receipt_dir",
                ),
                repo_root=resolved_repo_root,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result.outcome == "blocked":
            raise HTTPException(status_code=400, detail=result.errors[0])
        return {
            "retention_apply": result.to_record(),
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

    @app.post("/v1/validation-runs")
    async def validation_runs(request: dict[str, Any]) -> dict[str, Any]:
        scope = str(request.get("scope") or "").strip()
        if not scope:
            raise HTTPException(status_code=400, detail="scope is required")
        tier = str(request.get("tier") or "scoped").strip()
        manifest_path = str(request.get("manifest_path") or DEFAULT_MANIFEST_PATH).strip()
        actor = str(request.get("actor") or "wgcf-api").strip()
        try:
            manifest_file = _resolve_manifest_path(resolved_repo_root, manifest_path)
            result = run_operator_validation_check(
                actor=actor,
                artifact_root=_resolve_local_path(
                    resolved_repo_root,
                    str(request.get("artifact_root") or DEFAULT_ARTIFACT_ROOT),
                    "artifact_root",
                ),
                ledger_path=_resolve_local_path(
                    resolved_repo_root,
                    str(request.get("ledger") or DEFAULT_LEDGER_PATH),
                    "ledger",
                ),
                manifest_path=manifest_file,
                receipt_dir=_resolve_local_path(
                    resolved_repo_root,
                    str(request.get("receipt_dir") or DEFAULT_RECEIPT_DIR),
                    "receipt_dir",
                ),
                repo_root=resolved_repo_root,
                target_scope=scope,
                tier=tier,
            )
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return result.to_record()

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

    @app.get("/v1/receipts/{receipt_id}")
    async def receipt_detail(
        receipt_id: str,
        receipt_dir: str = Query(DEFAULT_RECEIPT_DIR, description="Repo-local compact receipt directory."),
    ) -> dict[str, Any]:
        try:
            receipt_path = _resolve_local_path(resolved_repo_root, receipt_dir, "receipt_dir")
            inspection = inspect_control_receipt(receipt_id, receipt_dir=receipt_path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "inspection": inspection.to_record(),
        }

    @app.post("/v1/readiness/evaluate")
    async def readiness_evaluate(request: dict[str, Any]) -> dict[str, Any]:
        target = str(request.get("target") or "").strip()
        profile = str(request.get("profile") or "").strip()
        if not target:
            raise HTTPException(status_code=400, detail="target is required")
        if not profile:
            raise HTTPException(status_code=400, detail="profile is required")
        try:
            result = run_operator_readiness_evaluation(
                actor=str(request.get("actor") or "wgcf-api").strip(),
                ledger_path=_resolve_local_path(
                    resolved_repo_root,
                    str(request.get("ledger") or DEFAULT_LEDGER_PATH),
                    "ledger",
                ),
                profile=profile,
                receipt_dir=_resolve_local_path(
                    resolved_repo_root,
                    str(request.get("receipt_dir") or DEFAULT_RECEIPT_DIR),
                    "receipt_dir",
                ),
                repo_root=resolved_repo_root,
                target=target,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "readiness": result.to_record(),
        }

    @app.post("/v1/art/graph")
    async def art_graph(request: dict[str, Any]) -> dict[str, Any]:
        try:
            graph_projection = build_art_runtime_graph(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "graph": graph_projection.to_record(),
        }

    @app.post("/v1/art/readiness")
    async def art_readiness(request: dict[str, Any]) -> dict[str, Any]:
        context = request.get("context") if isinstance(request.get("context"), dict) else request
        try:
            readiness = evaluate_art_readiness(
                context,
                operation=str(request.get("operation") or "complete"),
                target_item_id=request.get("target_item_id"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "readiness": readiness.to_record(),
        }

    @app.post("/v1/art/evidence-packet")
    async def art_evidence_packet(request: dict[str, Any]) -> dict[str, Any]:
        receipts = request.get("receipts")
        item_ids = request.get("item_ids")
        changed_surfaces = request.get("changed_surfaces")
        if not isinstance(receipts, list):
            raise HTTPException(status_code=400, detail="receipts must be an array")
        if not isinstance(item_ids, list):
            raise HTTPException(status_code=400, detail="item_ids must be an array")
        if not isinstance(changed_surfaces, list):
            raise HTTPException(status_code=400, detail="changed_surfaces must be an array")
        try:
            packet = project_receipts_to_art_evidence_packet(
                receipts,
                changed_surfaces=changed_surfaces,
                completion_summary=str(request.get("completion_summary") or "").strip(),
                item_ids=item_ids,
                residual_follow_up=request.get("residual_follow_up") or (),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "evidence_packet": packet.to_record(),
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
