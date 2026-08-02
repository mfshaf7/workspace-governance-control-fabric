from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))
sys.path.insert(0, str(REPO_ROOT / "apps/worker/src"))

from control_fabric_core.worker import (
    controlled_proof_worker_activation_status,
    controlled_proof_worker_status_snapshot,
    worker_activation_status,
    worker_status_snapshot,
)
from tests.controlled_proof_fixtures import (
    WGCF_REVISION,
    controlled_worker_env,
    write_image_source_revision,
    write_owner_context,
)
from wgcf_worker.main import main, render_worker_status_human


class WorkerTests(TestCase):
    def test_worker_status_is_source_ready_without_runtime_connection(self) -> None:
        snapshot = worker_status_snapshot(REPO_ROOT)

        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["status"], "activity-worker-source-ready")
        self.assertEqual(snapshot["runtime_mode"], "build-admitted-disabled")
        self.assertTrue(snapshot["temporal"]["task_queue"])
        self.assertTrue(snapshot["temporal"]["ready_boundary"])
        self.assertFalse(snapshot["temporal"]["connects_to_temporal"])
        self.assertFalse(snapshot["temporal"]["long_running_worker"])
        self.assertEqual(snapshot["temporal"]["sdk_dependency"], "temporalio>=1.30,<2")
        self.assertFalse(snapshot["activation"]["authorized"])

    def test_worker_status_declares_only_bounded_implemented_activity(self) -> None:
        snapshot = worker_status_snapshot(REPO_ROOT)
        capabilities = {
            capability["capability_id"]: capability
            for capability in snapshot["capabilities"]
        }

        self.assertTrue(capabilities["validation-readiness"]["implemented"])
        self.assertFalse(capabilities["aggregate-workflow-control"]["implemented"])
        self.assertEqual(
            snapshot["temporal"]["registered_activities"],
            ["wgcf.validation-readiness.evaluate"],
        )
        self.assertEqual(
            snapshot["temporal"]["result_status_codes"],
            ["ready", "blocked", "timed-out", "unavailable"],
        )
        self.assertEqual(
            snapshot["temporal"]["failure_status_codes"],
            ["blocked", "retryable", "timed-out", "cancelled", "unavailable"],
        )

    def test_worker_status_uses_temporal_environment_names(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "WGCF_TEMPORAL_NAMESPACE": "devint",
                "WGCF_TEMPORAL_TASK_QUEUE": "wgcf.validation-readiness.v1",
                "WGCF_TEMPORAL_ADDRESS": "temporal.devint.local:7233",
            },
        ):
            snapshot = worker_status_snapshot(REPO_ROOT)

        self.assertEqual(snapshot["temporal"]["namespace"], "devint")
        self.assertEqual(
            snapshot["temporal"]["task_queue"],
            "wgcf.validation-readiness.v1",
        )
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

    def test_worker_run_refuses_default_disabled_posture(self) -> None:
        error = StringIO()
        with redirect_stdout(StringIO()), patch("sys.stderr", error):
            result = main(["run"])

        self.assertEqual(result, 2)
        self.assertIn("refused to start", error.getvalue())

    def test_worker_activation_requires_all_gates_and_exact_identity(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "WGCF_TEMPORAL_WORKER_ENABLED": "true",
                "WGCF_TEMPORAL_ACTIVITY_EXECUTION_AUTHORIZED": "true",
                "WGCF_TEMPORAL_ACTIVATION_REVIEW_REF": (
                    "security-review:temporal-activation"
                ),
                "WGCF_TEMPORAL_TASK_QUEUE": "wgcf.validation-readiness.v1",
                "WGCF_TEMPORAL_WORKER_ID": "wgcf-activity-worker",
            },
            clear=False,
        ):
            activation = worker_activation_status()

        self.assertTrue(activation["authorized"])
        self.assertEqual(activation["blockers"], [])

    def test_controlled_proof_status_is_separate_and_default_denied(self) -> None:
        snapshot = controlled_proof_worker_status_snapshot(REPO_ROOT)

        self.assertTrue(snapshot["ready"])
        self.assertFalse(snapshot["activation"]["authorized"])
        self.assertEqual(
            snapshot["temporal"]["task_queue"],
            "wgcf.controlled-proof.validation-readiness.v1",
        )
        self.assertEqual(
            snapshot["temporal"]["identity"],
            "wgcf-controlled-proof-activity-worker",
        )

    def test_controlled_proof_activation_requires_exact_owner_context(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            path, digest = write_owner_context(Path(temp_dir))
            source_revision_path = write_image_source_revision(Path(temp_dir))
            with patch.dict(
                "os.environ",
                controlled_worker_env(path, digest),
                clear=False,
            ):
                activation = controlled_proof_worker_activation_status(
                    now=datetime(2026, 8, 2, 0, 3, tzinfo=timezone.utc),
                    source_revision_path=source_revision_path,
                )

        self.assertTrue(activation["authorized"])
        self.assertEqual(activation["blockers"], [])
        self.assertEqual(
            activation["commissioning_session_id"],
            "commissioning-session-698-1",
        )

    def test_controlled_proof_activation_rejects_queue_and_source_drift(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            path, digest = write_owner_context(Path(temp_dir))
            source_revision_path = write_image_source_revision(
                Path(temp_dir),
                "e" * 40,
            )
            environment = controlled_worker_env(path, digest)
            environment["WGCF_CONTROLLED_PROOF_TEMPORAL_TASK_QUEUE"] = (
                "wgcf.validation-readiness.v1"
            )
            with patch.dict("os.environ", environment, clear=False):
                activation = controlled_proof_worker_activation_status(
                    now=datetime(2026, 8, 2, 0, 3, tzinfo=timezone.utc),
                    source_revision_path=source_revision_path,
                )

        self.assertFalse(activation["authorized"])
        self.assertTrue(
            any("task queue" in blocker for blocker in activation["blockers"]),
        )
        self.assertTrue(
            any(
                "image source revision" in blocker
                for blocker in activation["blockers"]
            ),
        )

    def test_controlled_proof_activation_ignores_a_deployment_revision_echo(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path, digest = write_owner_context(root)
            source_revision_path = write_image_source_revision(root)
            environment = controlled_worker_env(path, digest)
            environment["WGCF_CONTROLLED_PROOF_SOURCE_REVISION"] = "e" * 40
            with patch.dict("os.environ", environment, clear=False):
                activation = controlled_proof_worker_activation_status(
                    now=datetime(2026, 8, 2, 0, 3, tzinfo=timezone.utc),
                    source_revision_path=source_revision_path,
                )

        self.assertTrue(activation["authorized"])
        self.assertEqual(activation["image_source_revision"], WGCF_REVISION)

    def test_controlled_proof_activation_rejects_missing_image_provenance(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path, digest = write_owner_context(root)
            with patch.dict(
                "os.environ",
                controlled_worker_env(path, digest),
                clear=False,
            ):
                activation = controlled_proof_worker_activation_status(
                    now=datetime(2026, 8, 2, 0, 3, tzinfo=timezone.utc),
                    source_revision_path=root / "missing-source-revision",
                )

        self.assertFalse(activation["authorized"])
        self.assertIn(
            "the worker image source provenance is invalid",
            activation["blockers"],
        )

    def test_controlled_proof_activation_rejects_malformed_image_provenance(
        self,
    ) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path, digest = write_owner_context(root)
            source_revision_path = write_image_source_revision(
                root,
                "not-a-revision",
            )
            with patch.dict(
                "os.environ",
                controlled_worker_env(path, digest),
                clear=False,
            ):
                activation = controlled_proof_worker_activation_status(
                    now=datetime(2026, 8, 2, 0, 3, tzinfo=timezone.utc),
                    source_revision_path=source_revision_path,
                )

        self.assertFalse(activation["authorized"])
        self.assertIn(
            "the worker image source provenance is invalid",
            activation["blockers"],
        )

    def test_controlled_proof_activation_rejects_an_unwritable_evidence_root(
        self,
    ) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path, digest = write_owner_context(root)
            source_revision_path = write_image_source_revision(root)
            with (
                patch.dict(
                    "os.environ",
                    controlled_worker_env(path, digest),
                    clear=False,
                ),
                patch("control_fabric_core.worker.os.access", return_value=False),
            ):
                activation = controlled_proof_worker_activation_status(
                    now=datetime(2026, 8, 2, 0, 3, tzinfo=timezone.utc),
                    source_revision_path=source_revision_path,
                )

        self.assertFalse(activation["authorized"])
        self.assertIn(
            "the controlled-proof evidence root is not writable",
            activation["blockers"],
        )

    def test_controlled_proof_activation_rejects_a_future_session(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            path, digest = write_owner_context(Path(temp_dir))
            source_revision_path = write_image_source_revision(Path(temp_dir))
            with patch.dict(
                "os.environ",
                controlled_worker_env(path, digest),
                clear=False,
            ):
                activation = controlled_proof_worker_activation_status(
                    now=datetime(2026, 8, 1, tzinfo=timezone.utc),
                    source_revision_path=source_revision_path,
                )

        self.assertFalse(activation["authorized"])
        self.assertTrue(
            any("has not started" in blocker for blocker in activation["blockers"]),
        )

    def test_controlled_proof_cli_status_and_run_gate_are_distinct(self) -> None:
        buffer = StringIO()
        with redirect_stdout(buffer):
            result = main(
                ["controlled-proof", "status", "--repo-root", str(REPO_ROOT)],
            )
        self.assertEqual(result, 0)
        self.assertIn("WGCF Controlled Proof Activity Worker", buffer.getvalue())

        error = StringIO()
        with redirect_stdout(StringIO()), patch("sys.stderr", error):
            result = main(["controlled-proof", "run"])
        self.assertEqual(result, 2)
        self.assertIn("refused to start", error.getvalue())
