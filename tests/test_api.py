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
    app = create_app(REPO_ROOT)
    parsed_path = urlsplit(path)
    messages: list[dict[str, Any]] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": parsed_path.path,
        "raw_path": parsed_path.path.encode("ascii"),
        "query_string": parsed_path.query.encode("ascii"),
        "headers": [],
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
