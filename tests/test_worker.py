from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))
sys.path.insert(0, str(REPO_ROOT / "apps/worker/src"))

from control_fabric_core.worker import worker_status_snapshot
from wgcf_worker.main import main, render_worker_status_human


class WorkerTests(TestCase):
    def test_worker_status_is_temporal_ready_without_runtime_connection(self) -> None:
        snapshot = worker_status_snapshot(REPO_ROOT)

        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["status"], "worker-skeleton")
        self.assertEqual(snapshot["runtime_mode"], "local-scaffold")
        self.assertTrue(snapshot["temporal"]["task_queue"])
        self.assertTrue(snapshot["temporal"]["ready_boundary"])
        self.assertFalse(snapshot["temporal"]["connects_to_temporal"])
        self.assertFalse(snapshot["temporal"]["long_running_worker"])
        self.assertEqual(snapshot["temporal"]["sdk_dependency"], "deferred")

    def test_worker_status_declares_future_capabilities_without_implementing_them(self) -> None:
        snapshot = worker_status_snapshot(REPO_ROOT)
        capabilities = {
            capability["capability_id"]: capability
            for capability in snapshot["capabilities"]
        }

        self.assertIn("source-snapshot-ingest", capabilities)
        self.assertIn("validation-plan-execute", capabilities)
        self.assertIn("control-receipt-append", capabilities)
        self.assertFalse(any(capability["implemented"] for capability in capabilities.values()))

    def test_worker_status_uses_temporal_environment_names(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "WGCF_TEMPORAL_NAMESPACE": "devint",
                "WGCF_TEMPORAL_TASK_QUEUE": "wgcf-devint",
                "WGCF_TEMPORAL_ADDRESS": "temporal.devint.local:7233",
            },
        ):
            snapshot = worker_status_snapshot(REPO_ROOT)

        self.assertEqual(snapshot["temporal"]["namespace"], "devint")
        self.assertEqual(snapshot["temporal"]["task_queue"], "wgcf-devint")
        self.assertEqual(snapshot["temporal"]["address"], "temporal.devint.local:7233")

    def test_worker_human_status_is_compact(self) -> None:
        rendered = render_worker_status_human(worker_status_snapshot(REPO_ROOT))

        self.assertIn("Workspace Governance Control Fabric Worker", rendered)
        self.assertIn("ready: true", rendered)
        self.assertIn("temporal-ready boundary: true", rendered)
        self.assertIn("connects to temporal: false", rendered)
        self.assertNotIn("Traceback", rendered)

    def test_worker_cli_status_returns_zero(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            result = main(["status", "--repo-root", str(REPO_ROOT)])

        self.assertEqual(result, 0)
        self.assertIn("long-running worker: false", buffer.getvalue())

    def test_worker_cli_status_json_is_serializable(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            result = main(["status", "--repo-root", str(REPO_ROOT), "--json"])

        self.assertEqual(result, 0)
        payload = json.loads(buffer.getvalue())
        self.assertTrue(payload["ready"])
        self.assertFalse(payload["temporal"]["connects_to_temporal"])
