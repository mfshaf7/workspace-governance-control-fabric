from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps/api/src"))
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import (
    MAX_AGENT_ACTION_EVALUATION_REQUEST_BYTES,
    MAX_DELIVERY_ART_READINESS_REQUEST_BYTES,
    MAX_PROTOTYPE_INGRESS_READINESS_REQUEST_BYTES,
    MAX_REPOSITORY_READINESS_REQUEST_BYTES,
    MAX_REGISTRY_REQUEST_BYTES,
    ArtifactRegistryAuthorizer,
    PACKAGE_VERSION,
)
from control_fabric_core.canonical_json import canonical_digest
from wgcf_api import create_app


async def asgi_get_json(path: str) -> tuple[int, dict[str, Any]]:
    return await asgi_request_json("GET", path)


async def asgi_post_json(path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return await asgi_request_json("POST", path, payload)


async def asgi_request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    app: Any | None = None,
    headers: dict[str, str] | None = None,
    raw_body: bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    resolved_app = app or create_app(REPO_ROOT)
    parsed_path = urlsplit(path)
    body = raw_body if raw_body is not None else json.dumps(payload or {}).encode("utf-8")
    messages: list[dict[str, Any]] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    request_headers = {
        "content-type": "application/json",
        "content-length": str(len(body)),
        **(headers or {}),
    }
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": parsed_path.path,
        "raw_path": parsed_path.path.encode("ascii"),
        "query_string": parsed_path.query.encode("ascii"),
        "headers": [
            (name.lower().encode("ascii"), value.encode("ascii"))
            for name, value in request_headers.items()
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    await resolved_app(scope, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, json.loads(body.decode("utf-8"))


class StubRegistryResult:
    def __init__(self, record: dict[str, Any]) -> None:
        self._record = record

    def to_record(self) -> dict[str, Any]:
        return self._record


class StubArtifactRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, str]] = []

    def register(self, raw_request: bytes, *, actor: str) -> StubRegistryResult:
        self.calls.append(("register", raw_request, actor))
        return StubRegistryResult({"registry": {"resolution": "created"}})

    def read(self, content_digest: str, *, actor: str) -> StubRegistryResult:
        self.calls.append(("read", content_digest, actor))
        return StubRegistryResult({"registry": {"resolution": "read"}})

    def reconcile(self, content_digest: str, *, actor: str) -> StubRegistryResult:
        self.calls.append(("reconcile", content_digest, actor))
        return StubRegistryResult({"state": "consistent"})


class StubDeliveryArtReadiness:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, str]] = []

    def issue(self, raw_request: bytes, *, actor: str) -> StubRegistryResult:
        self.calls.append(("issue", raw_request, actor))
        return StubRegistryResult({"receipt": {"resolution": "created"}})

    def read(self, receipt_token: str, *, actor: str) -> StubRegistryResult:
        self.calls.append(("read", receipt_token, actor))
        return StubRegistryResult({"receipt": {"resolution": "read"}})


class StubPrototypeIngressReadiness(StubDeliveryArtReadiness):
    pass


class StubRepositoryReadiness(StubDeliveryArtReadiness):
    pass


class ApiTests(TestCase):
    def registry_app(self) -> tuple[Any, StubArtifactRegistry]:
        registry = StubArtifactRegistry()
        authorizer = ArtifactRegistryAuthorizer(
            oos_secret="o" * 32,
            reconciler_secret="r" * 32,
        )
        return (
            create_app(
                REPO_ROOT,
                artifact_registry=registry,
                artifact_registry_authorizer=authorizer,
            ),
            registry,
        )

    def readiness_app(self) -> tuple[Any, StubDeliveryArtReadiness]:
        registry = StubArtifactRegistry()
        readiness = StubDeliveryArtReadiness()
        authorizer = ArtifactRegistryAuthorizer(
            oos_secret="o" * 32,
            reconciler_secret="r" * 32,
        )
        return (
            create_app(
                REPO_ROOT,
                artifact_registry=registry,
                artifact_registry_authorizer=authorizer,
                delivery_art_readiness=readiness,
            ),
            readiness,
        )

    def prototype_readiness_app(self) -> tuple[Any, StubPrototypeIngressReadiness]:
        readiness = StubPrototypeIngressReadiness()
        authorizer = ArtifactRegistryAuthorizer(
            oos_secret="o" * 32,
            reconciler_secret="r" * 32,
        )
        return (
            create_app(
                REPO_ROOT,
                artifact_registry_authorizer=authorizer,
                prototype_ingress_readiness=readiness,
            ),
            readiness,
        )

    def repository_readiness_app(self) -> tuple[Any, StubRepositoryReadiness]:
        readiness = StubRepositoryReadiness()
        authorizer = ArtifactRegistryAuthorizer(
            oos_secret="o" * 32,
            reconciler_secret="r" * 32,
        )
        return (
            create_app(
                REPO_ROOT,
                artifact_registry_authorizer=authorizer,
                repository_readiness=readiness,
            ),
            readiness,
        )

    def agent_action_payload(self) -> dict[str, Any]:
        fixture_root = REPO_ROOT / "contracts/agent-action/fixtures"
        request = json.loads((fixture_root / "request.valid.json").read_text(encoding="utf-8"))
        current = json.loads((fixture_root / "current.valid.json").read_text(encoding="utf-8"))
        now = datetime.now(UTC)
        request["requested_at"] = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        request["expires_at"] = (now + timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
        request["integrity"].pop("content_digest")
        request["integrity"]["content_digest"] = canonical_digest(request)
        current["approval_expires_at"] = (now + timedelta(minutes=10)).isoformat().replace(
            "+00:00",
            "Z",
        )
        return {"request": request, "current": current}

    def test_agent_action_evaluation_is_authenticated_and_compact(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            ledger_path = Path(temp_dir) / "ledger.jsonl"
            authorizer = ArtifactRegistryAuthorizer(
                oos_secret="o" * 32,
                reconciler_secret="r" * 32,
            )
            app = create_app(
                REPO_ROOT,
                artifact_registry_authorizer=authorizer,
                agent_action_ledger_path=ledger_path,
            )
            status, payload = asyncio.run(
                asgi_request_json(
                    "POST",
                    "/v1/agent-actions/evaluate",
                    self.agent_action_payload(),
                    app=app,
                    headers={
                        "x-wgcf-caller-id": "operator-orchestration-service",
                        "x-wgcf-caller-secret": "o" * 32,
                    },
                ),
            )

            self.assertEqual(status, 200)
            self.assertEqual(payload["evaluation"]["decision"]["outcome"], "allow")
            self.assertEqual(
                payload["evaluation"]["ledger_event"]["actor"],
                "operator-orchestration-service",
            )
            self.assertTrue(ledger_path.is_file())
            self.assertNotIn("raw_context", json.dumps(payload))

    def test_agent_action_evaluation_rejects_unauthenticated_and_oversized_requests(self) -> None:
        authorizer = ArtifactRegistryAuthorizer(
            oos_secret="o" * 32,
            reconciler_secret="r" * 32,
        )
        app = create_app(REPO_ROOT, artifact_registry_authorizer=authorizer)
        status, _ = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/agent-actions/evaluate",
                self.agent_action_payload(),
                app=app,
            ),
        )
        self.assertEqual(status, 401)

        oversized = b"{" + b" " * MAX_AGENT_ACTION_EVALUATION_REQUEST_BYTES + b"}"
        status, _ = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/agent-actions/evaluate",
                app=app,
                headers={
                    "x-wgcf-caller-id": "operator-orchestration-service",
                    "x-wgcf-caller-secret": "o" * 32,
                },
                raw_body=oversized,
            ),
        )
        self.assertEqual(status, 413)

    def test_healthz_returns_service_version(self) -> None:
        status, payload = asyncio.run(asgi_get_json("/healthz"))

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["version"], PACKAGE_VERSION)

    def test_readyz_reports_scaffold_checks(self) -> None:
        status, payload = asyncio.run(asgi_get_json("/readyz"))

        self.assertEqual(status, 200)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["status"], "ready")
        self.assertIn("pyproject.toml", payload["checks"])

    def test_status_keeps_authority_reference_and_version(self) -> None:
        status, payload = asyncio.run(asgi_get_json("/v1/status"))

        self.assertEqual(status, 200)
        self.assertEqual(payload["version"], PACKAGE_VERSION)
        self.assertEqual(
            payload["authority_contract_ref"],
            "workspace-governance/contracts/governance-control-fabric-operator-surface.yaml",
        )

    def test_graph_returns_manifest_graph_summary(self) -> None:
        status, payload = asyncio.run(asgi_get_json("/v1/graph"))

        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["manifest_id"], "wgcf-bootstrap-manifest")
        self.assertGreater(payload["summary"]["node_count"], 0)
        self.assertIn("nodes", payload["graph"])

    def test_graph_query_returns_scope_slice(self) -> None:
        status, payload = asyncio.run(
            asgi_get_json("/v1/graph/query?scope=repo:workspace-governance-control-fabric&limit=1"),
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["query"]["scope"], "repo:workspace-governance-control-fabric")
        self.assertEqual(payload["query"]["summary"]["node_count"], 1)
        self.assertGreater(payload["query"]["summary"]["node_total_count"], 1)
        self.assertEqual(payload["query"]["budget_decision"]["invocation_class"], "inline-fast")

    def test_budgets_endpoint_exposes_invocation_class_contract(self) -> None:
        status, payload = asyncio.run(asgi_get_json("/v1/budgets?operation=draft.submit"))

        self.assertEqual(status, 200)
        self.assertEqual(payload["profile"], "developer")
        self.assertEqual(payload["budgets"][0]["operation"], "draft.submit")
        self.assertEqual(payload["budgets"][0]["invocation_class"], "receipt-check")
        self.assertEqual(
            payload["evaluation"]["recommended_action"],
            "verify_payload_digest_and_fresh_receipt",
        )

    def test_lifecycle_retention_plan_endpoint_is_dry_run(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            artifact = temp_path / "artifacts/stdout.txt"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("raw output", encoding="utf-8")
            status, payload = asyncio.run(
                asgi_post_json(
                    "/v1/lifecycle/retention-plan",
                    {
                        "artifact_root": str(temp_path / "artifacts"),
                        "ledger": str(temp_path / "ledger.jsonl"),
                        "profile": "ci",
                        "receipt_dir": str(temp_path / "receipts"),
                    },
                ),
            )

            self.assertEqual(status, 200)
            plan = payload["retention_plan"]
            self.assertEqual(plan["profile"], "ci")
            self.assertTrue(plan["requires_confirmation"])
            self.assertTrue(artifact.is_file())

    def test_lifecycle_retention_apply_requires_confirm(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            status, payload = asyncio.run(
                asgi_post_json(
                    "/v1/lifecycle/retention-apply",
                    {
                        "artifact_root": str(temp_path / "artifacts"),
                        "ledger": str(temp_path / "ledger.jsonl"),
                        "receipt_dir": str(temp_path / "receipts"),
                    },
                ),
            )

        self.assertEqual(status, 400)
        self.assertIn("confirmation", payload["detail"])

    def test_graph_rejects_manifest_path_escape(self) -> None:
        status, payload = asyncio.run(asgi_get_json("/v1/graph?manifest_path=../outside.json"))

        self.assertEqual(status, 400)
        self.assertIn("repository root", payload["detail"])

    def test_source_snapshot_status_returns_compact_snapshot(self) -> None:
        status, payload = asyncio.run(asgi_get_json("/v1/source-snapshots/status"))

        self.assertEqual(status, 200)
        snapshot = payload["source_snapshot"]
        self.assertTrue(snapshot["snapshot_id"].startswith("source-snapshot:"))
        self.assertGreater(snapshot["summary"]["authority_ref_count"], 0)
        self.assertIn("workspace-governance-control-fabric", snapshot["repos"])
        self.assertNotIn("digests", snapshot)
        self.assertNotIn("root_path", json.dumps(snapshot, sort_keys=True))

    def test_source_snapshot_status_rejects_workspace_escape(self) -> None:
        status, payload = asyncio.run(asgi_get_json("/v1/source-snapshots/status?workspace_root=/tmp"))

        self.assertEqual(status, 400)
        self.assertIn("workspace", payload["detail"])

    def test_validation_plan_endpoint_returns_compact_plan(self) -> None:
        status, payload = asyncio.run(
            asgi_post_json(
                "/v1/validation-plans",
                {
                    "scope": "repo:workspace-governance-control-fabric",
                    "tier": "smoke",
                },
            ),
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["plan"]["decision"]["outcome"], "planned")
        self.assertEqual(payload["plan"]["performance_budget"]["invocation_class"], "inline-fast")
        self.assertEqual(payload["plan"]["checks"][0]["validator_id"], "control-fabric-status-smoke")

    def test_receipts_endpoint_lists_empty_receipt_directory(self) -> None:
        status, payload = asyncio.run(asgi_get_json("/v1/receipts?receipt_dir=.wgcf/test-missing-receipts"))

        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["receipts"], [])

    def test_validation_run_and_receipt_detail_endpoints_use_compact_receipts(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            run_status, run_payload = asyncio.run(
                asgi_post_json(
                    "/v1/validation-runs",
                    {
                        "actor": "test-api",
                        "artifact_root": str(temp_path / "artifacts"),
                        "ledger": str(temp_path / "ledger.jsonl"),
                        "receipt_dir": str(temp_path / "receipts"),
                        "scope": "repo:workspace-governance-control-fabric",
                        "tier": "smoke",
                    },
                ),
            )

            self.assertEqual(run_status, 200)
            self.assertEqual(run_payload["receipt"]["outcome"], "success")
            self.assertTrue(run_payload["receipt"]["correlation_id"].startswith("correlation:validation:"))
            self.assertEqual(run_payload["receipt"]["metrics"]["check_count"], 1)
            self.assertFalse(run_payload["receipt"]["suppressed_output_summary"]["raw_output_in_receipt"])
            self.assertTrue(Path(run_payload["receipt_path"]).is_file())
            self.assertTrue((temp_path / "ledger.jsonl").is_file())

            receipt_id = quote(run_payload["receipt"]["receipt_id"], safe=":")
            receipt_dir = quote(str(temp_path / "receipts"), safe="")
            detail_status, detail_payload = asyncio.run(
                asgi_get_json(f"/v1/receipts/{receipt_id}?receipt_dir={receipt_dir}"),
            )

        self.assertEqual(detail_status, 200)
        inspection = detail_payload["inspection"]
        self.assertEqual(inspection["receipt"]["receipt_id"], run_payload["receipt"]["receipt_id"])
        self.assertEqual(inspection["receipt"]["correlation_id"], run_payload["receipt"]["correlation_id"])
        self.assertFalse(inspection["raw_output_embedded"])
        self.assertEqual(inspection["check_status_counts"]["success"], 1)

    def test_metrics_receipts_endpoint_summarizes_compact_receipts(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            run_status, _run_payload = asyncio.run(
                asgi_post_json(
                    "/v1/validation-runs",
                    {
                        "actor": "test-api",
                        "artifact_root": str(temp_path / "artifacts"),
                        "ledger": str(temp_path / "ledger.jsonl"),
                        "receipt_dir": str(temp_path / "receipts"),
                        "scope": "repo:workspace-governance-control-fabric",
                        "tier": "smoke",
                    },
                ),
            )
            receipt_dir = quote(str(temp_path / "receipts"), safe="")
            metrics_status, metrics_payload = asyncio.run(
                asgi_get_json(f"/v1/metrics/receipts?receipt_dir={receipt_dir}"),
            )

        self.assertEqual(run_status, 200)
        self.assertEqual(metrics_status, 200)
        self.assertEqual(metrics_payload["metrics"]["receipt_count"], 1)
        self.assertEqual(metrics_payload["metrics"]["outcome_counts"]["success"], 1)

    def test_readiness_evaluate_endpoint_records_local_ledger_event(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            status, payload = asyncio.run(
                asgi_post_json(
                    "/v1/readiness/evaluate",
                    {
                        "actor": "test-api",
                        "ledger": str(temp_path / "ledger.jsonl"),
                        "profile": "local-read-only",
                        "receipt_dir": str(temp_path / "receipts"),
                        "target": "operator-surface:wgcf-cli",
                    },
                ),
            )

            self.assertEqual(status, 200)
            self.assertTrue((temp_path / "ledger.jsonl").is_file())

        readiness = payload["readiness"]
        self.assertTrue(readiness["ready"])
        self.assertTrue(readiness["correlation_id"].startswith("correlation:readiness:"))
        self.assertTrue(readiness["metrics"]["ready"])
        self.assertEqual(readiness["ledger_event"]["action"], "readiness.decision.recorded")
        self.assertEqual(readiness["mutation_boundary"], "fabric-local decision record only")

    def test_readiness_evaluate_endpoint_blocks_unknown_targets(self) -> None:
        status, payload = asyncio.run(
            asgi_post_json(
                "/v1/readiness/evaluate",
                {
                    "profile": "local-read-only",
                    "target": "repo:not-a-governed-repo",
                },
            ),
        )

        self.assertEqual(status, 200)
        readiness = payload["readiness"]
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["outcome"], "blocked")
        self.assertIn("unknown repo target: not-a-governed-repo", readiness["reasons"])

    def test_art_readiness_endpoint_returns_blocking_projection_recommendation(self) -> None:
        status, payload = asyncio.run(
            asgi_post_json(
                "/v1/art/readiness",
                {
                    "context": {
                        "continuation_context": {
                            "summary": {"open_child_count": 1},
                            "target_item": {
                                "id": 517,
                                "type": "Feature",
                                "status": "ready",
                                "owner_repo": "workspace-governance-control-fabric",
                                "delivery_team": "Platform Architecture",
                                "iteration": "PI-2026-03 / Iteration 1",
                                "target_pi": "PI-2026-03",
                                "descriptionPresent": True,
                                "descriptionHeadings": [
                                    "What This Enables",
                                    "Benefit Hypothesis",
                                    "Scope Boundaries",
                                    "Evidence Expectation",
                                    "Execution Context",
                                    "Operator work notes",
                                ],
                            },
                        },
                        "projection_state": {"dirty": True},
                    },
                    "operation": "complete",
                    "target_item_id": 517,
                },
            ),
        )

        self.assertEqual(status, 200)
        readiness = payload["readiness"]
        self.assertFalse(readiness["mutation_allowed"])
        self.assertTrue(readiness["correlation_id"].startswith("correlation:art-readiness:"))
        self.assertFalse(readiness["metrics"]["mutation_allowed"])
        self.assertTrue(readiness["projection_sync_recommended"])
        self.assertEqual(readiness["recommendations"][0]["action"], "projection_sync")

    def test_art_evidence_packet_endpoint_returns_broker_safe_payload(self) -> None:
        status, payload = asyncio.run(
            asgi_post_json(
                "/v1/art/evidence-packet",
                {
                    "changed_surfaces": ["`surface`: changed."],
                    "completion_summary": "Completed source-backed work.",
                    "item_ids": [517],
                    "receipts": [
                        {
                            "captured_at": "2026-05-01T00:00:00Z",
                            "check_results": [
                                {
                                    "check_id": "unit",
                                    "exit_code": 0,
                                    "status": "success",
                                    "validator_id": "tests",
                                },
                            ],
                            "digest": "sha256:" + "a" * 64,
                            "outcome": "success",
                            "receipt_id": "control-receipt:aaaaaaaaaaaaaaaaaaaaaaaa",
                            "target_scope": "repo:workspace-governance-control-fabric",
                        },
                    ],
                },
            ),
        )

        self.assertEqual(status, 200)
        packet = payload["evidence_packet"]
        self.assertFalse(packet["raw_artifacts_embedded"])
        self.assertIn("- PASS:", packet["completion_payload"]["test_result_evidence"])

    def test_artifact_registry_routes_enforce_caller_scopes(self) -> None:
        app, registry = self.registry_app()
        oos_headers = {
            "x-wgcf-caller-id": "operator-orchestration-service",
            "x-wgcf-caller-secret": "o" * 32,
        }
        wgcf_headers = {
            "x-wgcf-caller-id": "workspace-governance-control-fabric",
            "x-wgcf-caller-secret": "r" * 32,
        }
        digest_hex = "a" * 64

        register_status, _ = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/artifacts/delivery-art",
                {"request": "bounded"},
                app=app,
                headers=oos_headers,
            ),
        )
        read_status, _ = asyncio.run(
            asgi_request_json(
                "GET",
                f"/v1/artifacts/delivery-art/{digest_hex}",
                app=app,
                headers=oos_headers,
            ),
        )
        denied_status, _ = asyncio.run(
            asgi_request_json(
                "POST",
                f"/v1/artifacts/delivery-art/{digest_hex}/reconcile",
                app=app,
                headers=oos_headers,
            ),
        )
        reconcile_status, payload = asyncio.run(
            asgi_request_json(
                "POST",
                f"/v1/artifacts/delivery-art/{digest_hex}/reconcile",
                app=app,
                headers=wgcf_headers,
            ),
        )

        self.assertEqual(register_status, 200)
        self.assertEqual(read_status, 200)
        self.assertEqual(denied_status, 403)
        self.assertEqual(reconcile_status, 200)
        self.assertEqual(payload["reconciliation"]["state"], "consistent")
        self.assertEqual(
            [(operation, actor) for operation, _, actor in registry.calls],
            [
                ("register", "operator-orchestration-service"),
                ("read", "operator-orchestration-service"),
                ("reconcile", "workspace-governance-control-fabric"),
            ],
        )
        self.assertEqual(registry.calls[1][1], f"sha256:{digest_hex}")

    def test_artifact_registry_rejects_missing_auth_and_oversized_body(self) -> None:
        app, registry = self.registry_app()
        missing_status, _ = asyncio.run(
            asgi_request_json("POST", "/v1/artifacts/delivery-art", {}, app=app),
        )
        oversized_status, _ = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/artifacts/delivery-art",
                app=app,
                headers={
                    "x-wgcf-caller-id": "operator-orchestration-service",
                    "x-wgcf-caller-secret": "o" * 32,
                },
                raw_body=b"x" * (MAX_REGISTRY_REQUEST_BYTES + 1),
            ),
        )
        negative_length_status, _ = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/artifacts/delivery-art",
                app=app,
                headers={
                    "content-length": "-1",
                    "x-wgcf-caller-id": "operator-orchestration-service",
                    "x-wgcf-caller-secret": "o" * 32,
                },
            ),
        )

        self.assertEqual(missing_status, 401)
        self.assertEqual(oversized_status, 413)
        self.assertEqual(negative_length_status, 400)
        self.assertEqual(registry.calls, [])

    def test_artifact_registry_fails_closed_when_runtime_pair_is_incomplete(self) -> None:
        app = create_app(REPO_ROOT, artifact_registry=StubArtifactRegistry())

        status, payload = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/artifacts/delivery-art",
                {},
                app=app,
                headers={
                    "x-wgcf-caller-id": "operator-orchestration-service",
                    "x-wgcf-caller-secret": "o" * 32,
                },
            ),
        )

        self.assertEqual(status, 503)
        self.assertEqual(payload["detail"], "artifact registry is unavailable")

    def test_delivery_art_readiness_routes_enforce_caller_scopes(self) -> None:
        app, readiness = self.readiness_app()
        oos_headers = {
            "x-wgcf-caller-id": "operator-orchestration-service",
            "x-wgcf-caller-secret": "o" * 32,
        }
        reconciler_headers = {
            "x-wgcf-caller-id": "workspace-governance-control-fabric",
            "x-wgcf-caller-secret": "r" * 32,
        }
        token = "a" * 24

        issue_status, issue_payload = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/readiness/delivery-art",
                {"request": "bounded"},
                app=app,
                headers=oos_headers,
            ),
        )
        denied_status, _ = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/readiness/delivery-art",
                {"request": "bounded"},
                app=app,
                headers=reconciler_headers,
            ),
        )
        read_status, read_payload = asyncio.run(
            asgi_request_json(
                "GET",
                f"/v1/readiness/delivery-art/{token}",
                app=app,
                headers=reconciler_headers,
            ),
        )

        self.assertEqual(issue_status, 200)
        self.assertEqual(issue_payload["receipt"]["resolution"], "created")
        self.assertEqual(denied_status, 403)
        self.assertEqual(read_status, 200)
        self.assertEqual(read_payload["receipt"]["resolution"], "read")
        self.assertEqual(
            [(operation, actor) for operation, _, actor in readiness.calls],
            [
                ("issue", "operator-orchestration-service"),
                ("read", "workspace-governance-control-fabric"),
            ],
        )

    def test_delivery_art_readiness_rejects_missing_auth_and_oversized_body(self) -> None:
        app, readiness = self.readiness_app()
        missing_status, _ = asyncio.run(
            asgi_request_json("POST", "/v1/readiness/delivery-art", {}, app=app),
        )
        oversized_status, _ = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/readiness/delivery-art",
                app=app,
                headers={
                    "x-wgcf-caller-id": "operator-orchestration-service",
                    "x-wgcf-caller-secret": "o" * 32,
                },
                raw_body=b"x" * (MAX_DELIVERY_ART_READINESS_REQUEST_BYTES + 1),
            ),
        )

        self.assertEqual(missing_status, 401)
        self.assertEqual(oversized_status, 413)
        self.assertEqual(readiness.calls, [])

    def test_prototype_ingress_readiness_routes_are_authenticated_and_bounded(self) -> None:
        app, readiness = self.prototype_readiness_app()
        oos_headers = {
            "x-wgcf-caller-id": "operator-orchestration-service",
            "x-wgcf-caller-secret": "o" * 32,
        }
        reconciler_headers = {
            "x-wgcf-caller-id": "workspace-governance-control-fabric",
            "x-wgcf-caller-secret": "r" * 32,
        }
        token = "b" * 24

        issue_status, issue_payload = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/readiness/prototype-ingress",
                {"request": "bounded"},
                app=app,
                headers=oos_headers,
            ),
        )
        denied_status, _ = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/readiness/prototype-ingress",
                {"request": "bounded"},
                app=app,
                headers=reconciler_headers,
            ),
        )
        read_status, read_payload = asyncio.run(
            asgi_request_json(
                "GET",
                f"/v1/readiness/prototype-ingress/{token}",
                app=app,
                headers=reconciler_headers,
            ),
        )
        oversized_status, _ = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/readiness/prototype-ingress",
                app=app,
                headers=oos_headers,
                raw_body=b"x" * (MAX_PROTOTYPE_INGRESS_READINESS_REQUEST_BYTES + 1),
            ),
        )

        self.assertEqual(200, issue_status)
        self.assertEqual("created", issue_payload["receipt"]["resolution"])
        self.assertEqual(403, denied_status)
        self.assertEqual(200, read_status)
        self.assertEqual("read", read_payload["receipt"]["resolution"])
        self.assertEqual(413, oversized_status)
        self.assertEqual(
            [
                ("issue", "operator-orchestration-service"),
                ("read", "workspace-governance-control-fabric"),
            ],
            [(operation, actor) for operation, _, actor in readiness.calls],
        )

    def test_prototype_ingress_readiness_fails_closed_without_caller_auth(self) -> None:
        app = create_app(
            REPO_ROOT,
            prototype_ingress_readiness=StubPrototypeIngressReadiness(),
        )

        status, _ = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/readiness/prototype-ingress",
                {"request": "bounded"},
                app=app,
            ),
        )

        self.assertEqual(503, status)

    def test_repository_readiness_routes_are_authenticated_and_bounded(self) -> None:
        app, readiness = self.repository_readiness_app()
        oos_headers = {
            "x-wgcf-caller-id": "operator-orchestration-service",
            "x-wgcf-caller-secret": "o" * 32,
        }
        reconciler_headers = {
            "x-wgcf-caller-id": "workspace-governance-control-fabric",
            "x-wgcf-caller-secret": "r" * 32,
        }
        console_headers = {
            "x-wgcf-caller-id": "governance-operations-console",
            "x-wgcf-caller-secret": "c" * 32,
        }
        token = "c" * 24

        issue_status, issue_payload = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/readiness/repositories",
                {"request": "bounded"},
                app=app,
                headers=oos_headers,
            ),
        )
        denied_status, _ = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/readiness/repositories",
                {"request": "bounded"},
                app=app,
                headers=reconciler_headers,
            ),
        )
        console_status, _ = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/readiness/repositories",
                {"request": "bounded"},
                app=app,
                headers=console_headers,
            ),
        )
        read_status, read_payload = asyncio.run(
            asgi_request_json(
                "GET",
                f"/v1/readiness/repositories/{token}",
                app=app,
                headers=reconciler_headers,
            ),
        )
        oversized_status, _ = asyncio.run(
            asgi_request_json(
                "POST",
                "/v1/readiness/repositories",
                app=app,
                headers=oos_headers,
                raw_body=b"x" * (MAX_REPOSITORY_READINESS_REQUEST_BYTES + 1),
            ),
        )

        self.assertEqual(200, issue_status)
        self.assertEqual("created", issue_payload["receipt"]["resolution"])
        self.assertEqual(403, denied_status)
        self.assertEqual(401, console_status)
        self.assertEqual(200, read_status)
        self.assertEqual("read", read_payload["receipt"]["resolution"])
        self.assertEqual(413, oversized_status)
        self.assertEqual(
            [
                ("issue", "operator-orchestration-service"),
                ("read", "workspace-governance-control-fabric"),
            ],
            [(operation, actor) for operation, _, actor in readiness.calls],
        )
