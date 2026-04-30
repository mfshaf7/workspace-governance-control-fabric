from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))
sys.path.insert(0, str(REPO_ROOT / "apps/cli/src"))

from control_fabric_core import AUTHORITY_CONTRACT_REF, status_snapshot
from wgcf_cli.main import main, render_status_human


class FoundationTests(TestCase):
    def test_status_snapshot_points_to_authority_contract(self) -> None:
        snapshot = status_snapshot(REPO_ROOT)

        self.assertTrue(snapshot["ready"])
        self.assertTrue(
            snapshot["required_paths"][
                "packages/control_fabric_core/src/control_fabric_core/graph_ingestion.py"
            ]
        )
        self.assertTrue(
            snapshot["required_paths"][
                "packages/control_fabric_core/src/control_fabric_core/graph_queries.py"
            ]
        )
        self.assertTrue(
            snapshot["required_paths"][
                "packages/control_fabric_core/src/control_fabric_core/validation_planning.py"
            ]
        )
        self.assertTrue(snapshot["required_paths"]["schemas/governance-manifest.schema.json"])
        self.assertTrue(snapshot["required_paths"]["examples/governance-manifest.example.json"])
        self.assertEqual(
            snapshot["authority_contract_ref"],
            "workspace-governance/contracts/governance-control-fabric-operator-surface.yaml",
        )
        self.assertEqual(snapshot["authority_contract_ref"], AUTHORITY_CONTRACT_REF)

    def test_status_snapshot_keeps_runtime_boundaries_explicit(self) -> None:
        snapshot = status_snapshot(REPO_ROOT)
        repos = {
            boundary["repo"]
            for boundary in snapshot["authority_boundaries"]
        }

        self.assertIn("workspace-governance", repos)
        self.assertIn("workspace-governance-control-fabric", repos)
        self.assertIn("platform-engineering", repos)
        self.assertIn("security-architecture", repos)
        self.assertIn("operator-orchestration-service", repos)

    def test_human_status_is_compact_and_operator_safe(self) -> None:
        rendered = render_status_human(status_snapshot(REPO_ROOT))

        self.assertIn("Workspace Governance Control Fabric", rendered)
        self.assertIn("ready: true", rendered)
        self.assertIn("authority:", rendered)
        self.assertNotIn("Traceback", rendered)

    def test_cli_status_returns_zero(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            result = main(["status", "--repo-root", str(REPO_ROOT)])

        self.assertEqual(result, 0)
        self.assertIn("ready: true", buffer.getvalue())

    def test_cli_status_json_is_serializable(self) -> None:
        snapshot = status_snapshot(REPO_ROOT)
        self.assertIsInstance(json.dumps(snapshot, sort_keys=True), str)

    def test_cli_status_accepts_command_local_json_flag(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            result = main(["status", "--repo-root", str(REPO_ROOT), "--json"])

        self.assertEqual(result, 0)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["ready"])
        self.assertNotIn("url", payload["database"])

    def test_cli_graph_query_returns_scope_slice_json(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            result = main(
                [
                    "graph",
                    "query",
                    "--repo-root",
                    str(REPO_ROOT),
                    "--scope",
                    "repo:workspace-governance-control-fabric",
                    "--json",
                ],
            )

        self.assertEqual(result, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["query"]["scope"], "repo:workspace-governance-control-fabric")
        self.assertGreater(payload["query"]["summary"]["node_count"], 0)

    def test_cli_graph_query_human_output_is_compact(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            result = main(
                [
                    "graph",
                    "query",
                    "--repo-root",
                    str(REPO_ROOT),
                    "--scope",
                    "component:control-fabric-core",
                ],
            )

        self.assertEqual(result, 0)
        self.assertIn("Workspace Governance Control Fabric Graph Query", buffer.getvalue())
        self.assertIn("component:control-fabric-core", buffer.getvalue())
