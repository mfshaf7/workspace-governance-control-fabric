from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps/api/src"))
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import PACKAGE_VERSION
from wgcf_api import create_app


async def asgi_get_json(path: str) -> tuple[int, dict[str, Any]]:
    return await asgi_request_json("GET", path)


async def asgi_post_json(path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return await asgi_request_json("POST", path, payload)


async def asgi_request_json(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    app = create_app(REPO_ROOT)
    parsed_path = urlsplit(path)
    body = json.dumps(payload or {}).encode("utf-8")
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

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": parsed_path.path,
        "raw_path": parsed_path.path.encode("ascii"),
        "query_string": parsed_path.query.encode("ascii"),
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    await app(scope, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, json.loads(body.decode("utf-8"))


class ApiTests(TestCase):
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
            asgi_get_json("/v1/graph/query?scope=repo:workspace-governance-control-fabric"),
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["query"]["scope"], "repo:workspace-governance-control-fabric")
        self.assertGreater(payload["query"]["summary"]["node_count"], 0)

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
        self.assertEqual(payload["plan"]["checks"][0]["validator_id"], "control-fabric-status-smoke")

    def test_receipts_endpoint_lists_empty_receipt_directory(self) -> None:
        status, payload = asyncio.run(asgi_get_json("/v1/receipts?receipt_dir=.wgcf/test-missing-receipts"))

        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["receipts"], [])

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
