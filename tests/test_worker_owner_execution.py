from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))
sys.path.insert(0, str(REPO_ROOT / "apps/worker/src"))

from control_fabric_core.orchestration_activities import (
    ValidationReadinessContractError,
)
from wgcf_worker.owner_execution import execute_owner_envelope


def valid_envelope() -> dict[str, object]:
    return {
        "schema_version": 1,
        "payload": {"schema_version": 1},
        "activity_context": {
            "activity_id": "activity:validation-readiness",
            "attempt": 1,
            "worker_id": "wgcf-worker:test",
            "workflow_id": "workflow:validation-readiness-698",
        },
    }


class OwnerExecutionProtocolTests(TestCase):
    def test_completed_owner_result_uses_bounded_protocol(self) -> None:
        with patch(
            "wgcf_worker.owner_execution.execute_validation_readiness_activity",
            return_value={"schema_version": 1, "status_code": "ready"},
        ):
            response = execute_owner_envelope(valid_envelope())

        self.assertEqual(
            response,
            {
                "schema_version": 1,
                "status": "completed",
                "result": {"schema_version": 1, "status_code": "ready"},
            },
        )

    def test_owner_failure_suppresses_raw_detail(self) -> None:
        with patch(
            "wgcf_worker.owner_execution.execute_validation_readiness_activity",
            side_effect=ValidationReadinessContractError("raw contract detail"),
        ):
            response = execute_owner_envelope(valid_envelope())

        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["failure"]["error_type"], "WGCF_CONTRACT_REJECTED")
        self.assertNotIn("raw contract detail", str(response))
