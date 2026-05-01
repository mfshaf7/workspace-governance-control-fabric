from __future__ import annotations

import json
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))
sys.path.insert(0, str(REPO_ROOT / "apps/cli/src"))

from control_fabric_core import (
    AUTHORITY_CONTRACT_REF,
    bootstrap_validation_contract,
    status_snapshot,
)
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

    def test_bootstrap_validation_contract_avoids_runtime_dependency_loop(self) -> None:
        contract = bootstrap_validation_contract(REPO_ROOT)
        snapshot = status_snapshot(REPO_ROOT)

        self.assertTrue(contract["bootstrap_validator_present"])
        self.assertFalse(contract["uses_wgcf_receipt_as_bootstrap_authority"])
        self.assertEqual(contract["runtime_self_validation_role"], "bootstrap-independent")
        self.assertIn("wgcf check", contract["forbidden_runtime_entrypoints"])
        self.assertEqual(snapshot["bootstrap_validation"], contract)

    def test_dockerfile_copies_bootstrap_validator_required_by_readyz(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("COPY scripts ./scripts", dockerfile)

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

    def test_cli_sources_snapshot_returns_compact_json(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            result = main(["sources", "snapshot", "--repo-root", str(REPO_ROOT), "--json"])

        self.assertEqual(result, 0)
        payload = json.loads(buffer.getvalue())
        snapshot = payload["source_snapshot"]
        self.assertTrue(snapshot["snapshot_id"].startswith("source-snapshot:"))
        self.assertGreater(snapshot["summary"]["authority_ref_count"], 0)
        self.assertIn("workspace-governance-control-fabric", snapshot["repos"])
        self.assertNotIn("digests", snapshot)
        self.assertNotIn("root_path", json.dumps(snapshot, sort_keys=True))

    def test_cli_sources_snapshot_human_output_is_compact(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            result = main(["sources", "snapshot", "--repo-root", str(REPO_ROOT)])

        self.assertEqual(result, 0)
        rendered = buffer.getvalue()
        self.assertIn("Workspace Governance Control Fabric Source Snapshot", rendered)
        self.assertIn("authority refs:", rendered)
        self.assertNotIn("Traceback", rendered)

    def test_cli_plan_returns_compact_validation_plan_json(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            result = main(
                [
                    "plan",
                    "--repo-root",
                    str(REPO_ROOT),
                    "--scope",
                    "repo:workspace-governance-control-fabric",
                    "--tier",
                    "smoke",
                    "--json",
                ],
            )

        self.assertEqual(result, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["plan"]["decision"]["outcome"], "planned")
        self.assertEqual(payload["plan"]["checks"][0]["validator_id"], "control-fabric-status-smoke")

    def test_cli_check_writes_receipt_and_receipts_list_reads_it(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            check_buffer = StringIO()
            with redirect_stdout(check_buffer):
                check_result = main(
                    [
                        "check",
                        "--repo-root",
                        str(REPO_ROOT),
                        "--scope",
                        "repo:workspace-governance-control-fabric",
                        "--tier",
                        "smoke",
                        "--artifact-root",
                        str(temp_path / "artifacts"),
                        "--receipt-dir",
                        str(temp_path / "receipts"),
                        "--ledger",
                        str(temp_path / "ledger.jsonl"),
                        "--json",
                    ],
                )

            self.assertEqual(check_result, 0)
            check_payload = json.loads(check_buffer.getvalue())
            self.assertEqual(check_payload["receipt"]["outcome"], "success")
            self.assertTrue(Path(check_payload["receipt_path"]).is_file())
            self.assertTrue(Path(check_payload["ledger_path"]).is_file())

            list_buffer = StringIO()
            with redirect_stdout(list_buffer):
                list_result = main(
                    [
                        "receipts",
                        "list",
                        "--repo-root",
                        str(REPO_ROOT),
                        "--receipt-dir",
                        str(temp_path / "receipts"),
                        "--json",
                    ],
                )

            self.assertEqual(list_result, 0)
            list_payload = json.loads(list_buffer.getvalue())
            self.assertEqual(list_payload["count"], 1)
            self.assertEqual(
                list_payload["receipts"][0]["receipt_id"],
                check_payload["receipt"]["receipt_id"],
            )
