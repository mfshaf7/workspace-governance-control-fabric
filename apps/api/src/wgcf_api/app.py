"""FastAPI health, status, and graph query surface for the control fabric."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request

from control_fabric_core import (
    AUTHORITY_CONTRACT_REF,
    MAX_AGENT_ACTION_EVALUATION_REQUEST_BYTES,
    MAX_DELIVERY_ART_READINESS_REQUEST_BYTES,
    MAX_PROTOTYPE_INGRESS_READINESS_REQUEST_BYTES,
    MAX_REPOSITORY_CUSTODY_READINESS_REQUEST_BYTES,
    MAX_REPOSITORY_LIFECYCLE_READINESS_REQUEST_BYTES,
    MAX_REPOSITORY_READINESS_REQUEST_BYTES,
    MAX_REGISTRY_REQUEST_BYTES,
    ArtifactRegistryAuthorizer,
    ArtifactRegistryConflict,
    ArtifactRegistryContractError,
    ArtifactRegistryError,
    ArtifactRegistryForbidden,
    ArtifactRegistryNotFound,
    ArtifactRegistryUnauthorized,
    ArtifactRegistryUnavailable,
    ArtifactStorageError,
    AgentActionContractError,
    AgentActionPolicyError,
    DEFAULT_ARTIFACT_ROOT,
    DEFAULT_LEDGER_EXPORT_DIR,
    DEFAULT_LEDGER_PATH,
    DEFAULT_RECEIPT_DIR,
    DEFAULT_RETENTION_PROFILE,
    PACKAGE_VERSION,
    RUNTIME_REPO,
    DeliveryArtifactRegistry,
    DeliveryArtReadinessContractError,
    DeliveryArtReadinessError,
    DeliveryArtReadinessNotFound,
    DeliveryArtReadinessService,
    DeliveryArtReadinessUnavailable,
    PrototypeIngressReadinessContractError,
    PrototypeIngressReadinessError,
    PrototypeIngressReadinessNotFound,
    PrototypeIngressReadinessService,
    PrototypeIngressReadinessUnavailable,
    RepositoryCustodyReadinessConflict,
    RepositoryCustodyReadinessError,
    RepositoryCustodyReadinessNotFound,
    RepositoryCustodyReadinessRequestError,
    RepositoryCustodyReadinessService,
    RepositoryCustodyReadinessUnavailable,
    RepositoryLifecycleReadinessConflict,
    RepositoryLifecycleReadinessError,
    RepositoryLifecycleReadinessNotFound,
    RepositoryLifecycleReadinessRequestError,
    RepositoryLifecycleReadinessService,
    RepositoryLifecycleReadinessUnavailable,
    RepositoryReadinessError,
    RepositoryReadinessNotFound,
    RepositoryReadinessRequestError,
    RepositoryReadinessService,
    RepositoryReadinessUnavailable,
    apply_retention_plan,
    build_art_runtime_graph,
    build_artifact_registry_runtime,
    build_delivery_art_readiness_runtime,
    build_prototype_ingress_readiness_runtime,
    build_repository_custody_readiness_runtime,
    build_repository_lifecycle_readiness_runtime,
    build_repository_readiness_runtime,
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
    receipt_metrics_snapshot,
    run_operator_readiness_evaluation,
    run_agent_action_evaluation,
    run_operator_validation_check,
    source_snapshot_status,
    status_snapshot,
)
from control_fabric_core.canonical_json import strict_json_loads


DEFAULT_MANIFEST_PATH = "examples/governance-manifest.example.json"


def create_app(
    repo_root: str | Path | None = None,
    *,
    artifact_registry: DeliveryArtifactRegistry | None = None,
    artifact_registry_authorizer: ArtifactRegistryAuthorizer | None = None,
    agent_action_ledger_path: str | Path | None = None,
    delivery_art_readiness: DeliveryArtReadinessService | None = None,
    prototype_ingress_readiness: PrototypeIngressReadinessService | None = None,
    repository_custody_readiness: RepositoryCustodyReadinessService | None = None,
    repository_lifecycle_readiness: RepositoryLifecycleReadinessService | None = None,
    repository_readiness: RepositoryReadinessService | None = None,
) -> FastAPI:
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
    resolved_artifact_registry = artifact_registry
    resolved_registry_authorizer = artifact_registry_authorizer
    resolved_delivery_art_readiness = delivery_art_readiness
    resolved_prototype_ingress_readiness = prototype_ingress_readiness
    resolved_repository_custody_readiness = repository_custody_readiness
    resolved_repository_lifecycle_readiness = repository_lifecycle_readiness
    resolved_repository_readiness = repository_readiness
    resolved_agent_action_ledger_path = Path(
        agent_action_ledger_path or resolved_repo_root / DEFAULT_LEDGER_PATH,
    ).resolve()

    def registry_runtime() -> tuple[DeliveryArtifactRegistry, ArtifactRegistryAuthorizer]:
        nonlocal resolved_artifact_registry, resolved_registry_authorizer
        if resolved_artifact_registry is None and resolved_registry_authorizer is None:
            resolved_artifact_registry, resolved_registry_authorizer = (
                build_artifact_registry_runtime()
            )
        if resolved_artifact_registry is None or resolved_registry_authorizer is None:
            raise ArtifactRegistryUnavailable(
                "artifact registry service and authorizer must be configured together",
            )
        return resolved_artifact_registry, resolved_registry_authorizer

    def readiness_runtime() -> tuple[DeliveryArtReadinessService, ArtifactRegistryAuthorizer]:
        nonlocal resolved_delivery_art_readiness
        registry, authorizer = registry_runtime()
        if resolved_delivery_art_readiness is None:
            resolved_delivery_art_readiness = build_delivery_art_readiness_runtime(registry)
        return resolved_delivery_art_readiness, authorizer

    def caller_authorizer() -> ArtifactRegistryAuthorizer:
        if resolved_registry_authorizer is not None:
            return resolved_registry_authorizer
        return ArtifactRegistryAuthorizer.from_environment()

    def prototype_readiness_runtime() -> tuple[
        PrototypeIngressReadinessService,
        ArtifactRegistryAuthorizer,
    ]:
        nonlocal resolved_prototype_ingress_readiness
        authorizer = caller_authorizer()
        if resolved_prototype_ingress_readiness is None:
            resolved_prototype_ingress_readiness = build_prototype_ingress_readiness_runtime()
        return resolved_prototype_ingress_readiness, authorizer

    def repository_readiness_runtime() -> tuple[
        RepositoryReadinessService,
        ArtifactRegistryAuthorizer,
    ]:
        nonlocal resolved_repository_readiness
        authorizer = caller_authorizer()
        if resolved_repository_readiness is None:
            resolved_repository_readiness = build_repository_readiness_runtime()
        return resolved_repository_readiness, authorizer

    def repository_custody_readiness_runtime() -> tuple[
        RepositoryCustodyReadinessService,
        ArtifactRegistryAuthorizer,
    ]:
        nonlocal resolved_repository_custody_readiness
        authorizer = caller_authorizer()
        if resolved_repository_custody_readiness is None:
            resolved_repository_custody_readiness = build_repository_custody_readiness_runtime()
        return resolved_repository_custody_readiness, authorizer

    def repository_lifecycle_readiness_runtime() -> tuple[
        RepositoryLifecycleReadinessService,
        ArtifactRegistryAuthorizer,
    ]:
        nonlocal resolved_repository_lifecycle_readiness
        authorizer = caller_authorizer()
        if resolved_repository_lifecycle_readiness is None:
            resolved_repository_lifecycle_readiness = build_repository_lifecycle_readiness_runtime()
        return resolved_repository_lifecycle_readiness, authorizer

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

    @app.get("/v1/metrics/receipts")
    async def metrics_receipts(
        receipt_dir: str = Query(DEFAULT_RECEIPT_DIR, description="Repo-local compact receipt directory."),
    ) -> dict[str, Any]:
        try:
            receipt_path = _resolve_local_path(resolved_repo_root, receipt_dir, "receipt_dir")
            receipts = [receipt.to_record() for receipt in list_control_receipts(receipt_path)]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "metrics": receipt_metrics_snapshot(receipts),
            "receipt_dir": str(receipt_path.relative_to(resolved_repo_root)),
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

    @app.post("/v1/agent-actions/evaluate")
    async def agent_action_evaluate(request: Request) -> dict[str, Any]:
        try:
            authorizer = caller_authorizer()
            caller_id, caller_secret = _registry_caller(request)
            authorizer.authorize(caller_id, caller_secret, "evaluate-agent-action")
            raw_request = await _read_bounded_request(
                request,
                limit=MAX_AGENT_ACTION_EVALUATION_REQUEST_BYTES,
                label="agent-action evaluation request",
            )
            payload = strict_json_loads(raw_request)
            if not isinstance(payload, dict):
                raise AgentActionContractError("agent-action evaluation payload must be an object")
            action_request = payload.get("request")
            current = payload.get("current")
            if not isinstance(action_request, dict):
                raise AgentActionContractError("request must be an object")
            if not isinstance(current, dict):
                raise AgentActionContractError("current must be an object")
            current = copy.deepcopy(current)
            current["caller_workload_id"] = caller_id
            result = run_agent_action_evaluation(
                action_request,
                actor=caller_id,
                current=current,
                ledger_path=resolved_agent_action_ledger_path,
            )
        except (ArtifactRegistryUnauthorized, ArtifactRegistryForbidden) as exc:
            raise _artifact_registry_http_exception(exc) from exc
        except (AgentActionContractError, AgentActionPolicyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "evaluation": result.to_record(),
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

    @app.post("/v1/artifacts/delivery-art")
    async def register_delivery_art_artifact(request: Request) -> dict[str, Any]:
        try:
            registry, authorizer = registry_runtime()
            caller_id, caller_secret = _registry_caller(request)
            authorizer.authorize(caller_id, caller_secret, "register")
            raw_request = await _read_bounded_registry_request(request)
            return registry.register(raw_request, actor=caller_id).to_record()
        except HTTPException:
            raise
        except (ArtifactRegistryError, ArtifactStorageError) as exc:
            raise _artifact_registry_http_exception(exc) from exc

    @app.get("/v1/artifacts/delivery-art/{digest_hex}")
    async def read_delivery_art_artifact(digest_hex: str, request: Request) -> dict[str, Any]:
        try:
            registry, authorizer = registry_runtime()
            caller_id, caller_secret = _registry_caller(request)
            authorizer.authorize(caller_id, caller_secret, "read")
            return registry.read(f"sha256:{digest_hex}", actor=caller_id).to_record()
        except (ArtifactRegistryError, ArtifactStorageError) as exc:
            raise _artifact_registry_http_exception(exc) from exc

    @app.post("/v1/artifacts/delivery-art/{digest_hex}/reconcile")
    async def reconcile_delivery_art_artifact(
        digest_hex: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            registry, authorizer = registry_runtime()
            caller_id, caller_secret = _registry_caller(request)
            authorizer.authorize(caller_id, caller_secret, "reconcile")
            return {
                "reconciliation": registry.reconcile(
                    f"sha256:{digest_hex}",
                    actor=caller_id,
                ).to_record(),
            }
        except (ArtifactRegistryError, ArtifactStorageError) as exc:
            raise _artifact_registry_http_exception(exc) from exc

    @app.post("/v1/readiness/delivery-art")
    async def issue_delivery_art_readiness(request: Request) -> dict[str, Any]:
        try:
            service, authorizer = readiness_runtime()
            caller_id, caller_secret = _registry_caller(request)
            authorizer.authorize(caller_id, caller_secret, "evaluate-readiness")
            raw_request = await _read_bounded_request(
                request,
                limit=MAX_DELIVERY_ART_READINESS_REQUEST_BYTES,
                label="readiness request",
            )
            return service.issue(raw_request, actor=caller_id).to_record()
        except HTTPException:
            raise
        except (ArtifactRegistryError, ArtifactStorageError) as exc:
            raise _artifact_registry_http_exception(exc) from exc
        except DeliveryArtReadinessError as exc:
            raise _delivery_art_readiness_http_exception(exc) from exc

    @app.get("/v1/readiness/delivery-art/{receipt_token}")
    async def read_delivery_art_readiness(
        receipt_token: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            service, authorizer = readiness_runtime()
            caller_id, caller_secret = _registry_caller(request)
            authorizer.authorize(caller_id, caller_secret, "read-readiness")
            return service.read(receipt_token, actor=caller_id).to_record()
        except (ArtifactRegistryError, ArtifactStorageError) as exc:
            raise _artifact_registry_http_exception(exc) from exc
        except DeliveryArtReadinessError as exc:
            raise _delivery_art_readiness_http_exception(exc) from exc

    @app.post("/v1/readiness/prototype-ingress")
    async def issue_prototype_ingress_readiness(request: Request) -> dict[str, Any]:
        try:
            service, authorizer = prototype_readiness_runtime()
            caller_id, caller_secret = _registry_caller(request)
            authorizer.authorize(caller_id, caller_secret, "evaluate-readiness")
            raw_request = await _read_bounded_request(
                request,
                limit=MAX_PROTOTYPE_INGRESS_READINESS_REQUEST_BYTES,
                label="Prototype ingress readiness request",
            )
            return service.issue(raw_request, actor=caller_id).to_record()
        except ArtifactRegistryError as exc:
            raise _artifact_registry_http_exception(exc) from exc
        except PrototypeIngressReadinessError as exc:
            raise _prototype_ingress_readiness_http_exception(exc) from exc

    @app.get("/v1/readiness/prototype-ingress/{receipt_token}")
    async def read_prototype_ingress_readiness(
        receipt_token: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            service, authorizer = prototype_readiness_runtime()
            caller_id, caller_secret = _registry_caller(request)
            authorizer.authorize(caller_id, caller_secret, "read-readiness")
            return service.read(receipt_token, actor=caller_id).to_record()
        except ArtifactRegistryError as exc:
            raise _artifact_registry_http_exception(exc) from exc
        except PrototypeIngressReadinessError as exc:
            raise _prototype_ingress_readiness_http_exception(exc) from exc

    @app.post("/v1/readiness/repositories")
    async def issue_repository_readiness(request: Request) -> dict[str, Any]:
        try:
            service, authorizer = repository_readiness_runtime()
            caller_id, caller_secret = _registry_caller(request)
            authorizer.authorize(caller_id, caller_secret, "evaluate-readiness")
            raw_request = await _read_bounded_request(
                request,
                limit=MAX_REPOSITORY_READINESS_REQUEST_BYTES,
                label="repository readiness request",
            )
            return service.issue(raw_request, actor=caller_id).to_record()
        except ArtifactRegistryError as exc:
            raise _artifact_registry_http_exception(exc) from exc
        except RepositoryReadinessError as exc:
            raise _repository_readiness_http_exception(exc) from exc

    @app.post("/v1/readiness/repository-custody")
    async def issue_repository_custody_readiness(request: Request) -> dict[str, Any]:
        try:
            service, authorizer = repository_custody_readiness_runtime()
            caller_id, caller_secret = _registry_caller(request)
            authorizer.authorize(caller_id, caller_secret, "evaluate-readiness")
            raw_request = await _read_bounded_request(
                request,
                limit=MAX_REPOSITORY_CUSTODY_READINESS_REQUEST_BYTES,
                label="repository custody readiness request",
            )
            return service.issue(raw_request, actor=caller_id).to_record()
        except ArtifactRegistryError as exc:
            raise _artifact_registry_http_exception(exc) from exc
        except RepositoryCustodyReadinessError as exc:
            raise _repository_custody_readiness_http_exception(exc) from exc

    @app.get("/v1/readiness/repository-custody/{decision_token}")
    async def read_repository_custody_readiness(
        decision_token: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            service, authorizer = repository_custody_readiness_runtime()
            caller_id, caller_secret = _registry_caller(request)
            authorizer.authorize(caller_id, caller_secret, "read-readiness")
            return service.read(decision_token, actor=caller_id).to_record()
        except ArtifactRegistryError as exc:
            raise _artifact_registry_http_exception(exc) from exc
        except RepositoryCustodyReadinessError as exc:
            raise _repository_custody_readiness_http_exception(exc) from exc

    @app.post("/v1/readiness/repository-lifecycle")
    async def issue_repository_lifecycle_readiness(request: Request) -> dict[str, Any]:
        try:
            service, authorizer = repository_lifecycle_readiness_runtime()
            caller_id, caller_secret = _registry_caller(request)
            authorizer.authorize(caller_id, caller_secret, "evaluate-readiness")
            raw_request = await _read_bounded_request(
                request,
                limit=MAX_REPOSITORY_LIFECYCLE_READINESS_REQUEST_BYTES,
                label="repository lifecycle readiness request",
            )
            return service.issue(raw_request, actor=caller_id).to_record()
        except ArtifactRegistryError as exc:
            raise _artifact_registry_http_exception(exc) from exc
        except RepositoryLifecycleReadinessError as exc:
            raise _repository_lifecycle_readiness_http_exception(exc) from exc

    @app.get("/v1/readiness/repository-lifecycle/{decision_token}")
    async def read_repository_lifecycle_readiness(
        decision_token: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            service, authorizer = repository_lifecycle_readiness_runtime()
            caller_id, caller_secret = _registry_caller(request)
            authorizer.authorize(caller_id, caller_secret, "read-readiness")
            return service.read(decision_token, actor=caller_id).to_record()
        except ArtifactRegistryError as exc:
            raise _artifact_registry_http_exception(exc) from exc
        except RepositoryLifecycleReadinessError as exc:
            raise _repository_lifecycle_readiness_http_exception(exc) from exc

    @app.get("/v1/readiness/repositories/{receipt_token}")
    async def read_repository_readiness(
        receipt_token: str,
        request: Request,
    ) -> dict[str, Any]:
        try:
            service, authorizer = repository_readiness_runtime()
            caller_id, caller_secret = _registry_caller(request)
            authorizer.authorize(caller_id, caller_secret, "read-readiness")
            return service.read(receipt_token, actor=caller_id).to_record()
        except ArtifactRegistryError as exc:
            raise _artifact_registry_http_exception(exc) from exc
        except RepositoryReadinessError as exc:
            raise _repository_readiness_http_exception(exc) from exc

    return app


def _registry_caller(request: Request) -> tuple[str, str]:
    return (
        request.headers.get("x-wgcf-caller-id", "").strip(),
        request.headers.get("x-wgcf-caller-secret", ""),
    )


async def _read_bounded_registry_request(request: Request) -> bytes:
    return await _read_bounded_request(
        request,
        limit=MAX_REGISTRY_REQUEST_BYTES,
        label="registry request",
    )


async def _read_bounded_request(
    request: Request,
    *,
    limit: int,
    label: str,
) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            parsed_content_length = int(content_length)
            if parsed_content_length < 0:
                raise HTTPException(status_code=400, detail="invalid content-length header")
            if parsed_content_length > limit:
                raise HTTPException(status_code=413, detail=f"{label} exceeds payload limit")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content-length header") from exc
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail=f"{label} exceeds payload limit")
        body.extend(chunk)
    return bytes(body)


def _artifact_registry_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, ArtifactRegistryUnauthorized):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, ArtifactRegistryForbidden):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ArtifactRegistryNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ArtifactRegistryConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ArtifactRegistryContractError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=503, detail="artifact registry is unavailable")


def _delivery_art_readiness_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, DeliveryArtReadinessNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, DeliveryArtReadinessContractError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, DeliveryArtReadinessUnavailable):
        return HTTPException(status_code=503, detail="Delivery ART readiness is unavailable")
    return HTTPException(status_code=503, detail="Delivery ART readiness is unavailable")


def _prototype_ingress_readiness_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, PrototypeIngressReadinessNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PrototypeIngressReadinessContractError):
        return HTTPException(
            status_code=400,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, PrototypeIngressReadinessUnavailable):
        return HTTPException(status_code=503, detail="Prototype ingress readiness is unavailable")
    return HTTPException(status_code=503, detail="Prototype ingress readiness is unavailable")


def _repository_readiness_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, RepositoryReadinessNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RepositoryReadinessRequestError):
        return HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)})
    if isinstance(exc, RepositoryReadinessUnavailable):
        return HTTPException(status_code=503, detail="repository readiness is unavailable")
    return HTTPException(status_code=503, detail="repository readiness is unavailable")


def _repository_custody_readiness_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, RepositoryCustodyReadinessNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RepositoryCustodyReadinessConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RepositoryCustodyReadinessRequestError):
        return HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)})
    if isinstance(exc, RepositoryCustodyReadinessUnavailable):
        return HTTPException(status_code=503, detail="repository custody readiness is unavailable")
    return HTTPException(status_code=503, detail="repository custody readiness is unavailable")


def _repository_lifecycle_readiness_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, RepositoryLifecycleReadinessNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RepositoryLifecycleReadinessConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RepositoryLifecycleReadinessRequestError):
        return HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)})
    if isinstance(exc, RepositoryLifecycleReadinessUnavailable):
        return HTTPException(status_code=503, detail="repository lifecycle readiness is unavailable")
    return HTTPException(status_code=503, detail="repository lifecycle readiness is unavailable")


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
