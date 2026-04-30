from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase

from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps/api/src"))
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import PACKAGE_VERSION
from wgcf_api import create_app


class ApiTests(TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app(REPO_ROOT))

    def test_healthz_returns_service_version(self) -> None:
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["version"], PACKAGE_VERSION)

    def test_readyz_reports_scaffold_checks(self) -> None:
        response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["status"], "ready")
        self.assertIn("pyproject.toml", payload["checks"])

    def test_status_keeps_authority_reference_and_version(self) -> None:
        response = self.client.get("/v1/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["version"], PACKAGE_VERSION)
        self.assertEqual(
            payload["authority_contract_ref"],
            "workspace-governance/contracts/governance-control-fabric-operator-surface.yaml",
        )
