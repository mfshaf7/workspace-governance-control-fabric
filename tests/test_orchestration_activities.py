from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core.orchestration_activities import (
    VALIDATION_READINESS_ACTIVITY_NAME,
    ValidationReadinessActivityContext,
    ValidationReadinessActivityRequest,
    ValidationReadinessContractError,
    ValidationReadinessIdempotencyConflict,
    execute_validation_readiness_activity,
)


def valid_request() -> dict[str, object]:
    return {
        "schema_version": 1,
        "definition_id": "validation-readiness-run",
        "definition_version": 1,
        "run_id": "run:698-proof-01",
        "workflow_id": "workflow:validation-readiness-698",
        "source_ref": "art:delivery-698",
        "source_version": "commit:abc1234",
        "validation_scope": "component:workspace-governance",
        "readiness_target": "repo:workspace-governance-control-fabric",
        "profile": "local-read-only",
        "tier": "smoke",
        "correlation_id": "correlation:delivery-698",
        "causation_id": "causation:delivery-714",
        "idempotency_key": "idempotency:delivery-698-proof-01",
        "caller_id": "operator-orchestration-service",
        "operator_id": "operator:test",
    }


def validation_result(outcome: str = "success") -> SimpleNamespace:
    receipt = SimpleNamespace(
        receipt_id="control-receipt:0123456789abcdef01234567",
        digest=f"sha256:{'1' * 64}",
        outcome=outcome,
        target_scope="component:workspace-governance",
        tier="smoke",
    )
    return SimpleNamespace(
        receipt=receipt,
        check_result=SimpleNamespace(
            ledger_event=SimpleNamespace(
                event_id="ledger-event:validation012345678901",
            ),
        ),
    )


def readiness_result(ready: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        decision=SimpleNamespace(
            decision_id="readiness-decision:0123456789abcdef01234567",
            outcome="ready" if ready else "blocked",
            ready=ready,
            reasons=() if ready else ("bounded reason",),
        ),
        ledger_event=SimpleNamespace(
            event_id="ledger-event:readiness012345678901",
        ),
    )


class OrchestrationActivityTests(TestCase):
    def test_request_rejects_unknown_and_authority_expanding_fields(self) -> None:
        payload = valid_request()
        payload["raw_output"] = "not allowed"

        with self.assertRaisesRegex(
            ValidationReadinessContractError,
            "unknown fields: raw_output",
        ):
            ValidationReadinessActivityRequest.from_payload(payload)

    def test_request_is_locked_to_safe_proof_scope(self) -> None:
        payload = valid_request()
        payload["profile"] = "dev-integration"

        with self.assertRaisesRegex(
            ValidationReadinessContractError,
            "profile must be 'local-read-only'",
        ):
            ValidationReadinessActivityRequest.from_payload(payload)

    def test_execution_rejects_spoofed_workflow_identity(self) -> None:
        with self.assertRaisesRegex(
            ValidationReadinessContractError,
            "must match the Temporal execution context",
        ):
            execute_validation_readiness_activity(
                valid_request(),
                activity_context=ValidationReadinessActivityContext(
                    activity_id="activity:validation-readiness",
                    attempt=1,
                    worker_id="wgcf-activity-worker",
                    workflow_id="workflow:different",
                ),
                evidence_root="/tmp/not-used",
                repo_root=REPO_ROOT,
                workspace_root=REPO_ROOT.parent,
            )

    def test_execution_returns_compact_evidence_and_reuses_completed_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wgcf-activity-") as temp_dir:
            evidence_root = Path(temp_dir) / "evidence"
            with (
                patch(
                    "control_fabric_core.orchestration_activities."
                    "run_catalog_operator_validation_check",
                    return_value=validation_result(),
                ) as validation_mock,
                patch(
                    "control_fabric_core.orchestration_activities."
                    "run_operator_readiness_evaluation",
                    return_value=readiness_result(),
                ) as readiness_mock,
            ):
                result = execute_validation_readiness_activity(
                    valid_request(),
                    activity_context=ValidationReadinessActivityContext(
                        activity_id="activity:validation-readiness",
                        attempt=1,
                        worker_id="wgcf-activity-worker",
                        workflow_id="workflow:validation-readiness-698",
                    ),
                    evidence_root=evidence_root,
                    repo_root=REPO_ROOT,
                    workspace_root=REPO_ROOT.parent,
                )
                replay = execute_validation_readiness_activity(
                    valid_request(),
                    activity_context=ValidationReadinessActivityContext(
                        activity_id="activity:validation-readiness",
                        attempt=2,
                        worker_id="wgcf-activity-worker",
                        workflow_id="workflow:validation-readiness-698",
                    ),
                    evidence_root=evidence_root,
                    repo_root=REPO_ROOT,
                    workspace_root=REPO_ROOT.parent,
                )

        self.assertEqual(result, replay)
        self.assertEqual(result["activity_name"], VALIDATION_READINESS_ACTIVITY_NAME)
        self.assertEqual(result["status_code"], "ready")
        self.assertTrue(result["bounded_decision"]["ready"])
        self.assertEqual(validation_mock.call_count, 1)
        self.assertEqual(readiness_mock.call_count, 1)
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn(str(REPO_ROOT), serialized)
        self.assertNotIn("raw_output", serialized)
        self.assertNotIn("artifact_root", serialized)

    def test_idempotency_key_collision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wgcf-activity-") as temp_dir:
            with (
                patch(
                    "control_fabric_core.orchestration_activities."
                    "run_catalog_operator_validation_check",
                    return_value=validation_result(),
                ),
                patch(
                    "control_fabric_core.orchestration_activities."
                    "run_operator_readiness_evaluation",
                    return_value=readiness_result(),
                ),
            ):
                execute_validation_readiness_activity(
                    valid_request(),
                    activity_context=ValidationReadinessActivityContext(
                        activity_id="activity:one",
                        attempt=1,
                        worker_id="wgcf-activity-worker",
                        workflow_id="workflow:validation-readiness-698",
                    ),
                    evidence_root=temp_dir,
                    repo_root=REPO_ROOT,
                    workspace_root=REPO_ROOT.parent,
                )
                changed = valid_request()
                changed["source_version"] = "commit:def5678"
                with self.assertRaises(ValidationReadinessIdempotencyConflict):
                    execute_validation_readiness_activity(
                        changed,
                        activity_context=ValidationReadinessActivityContext(
                            activity_id="activity:one",
                            attempt=2,
                            worker_id="wgcf-activity-worker",
                            workflow_id="workflow:validation-readiness-698",
                        ),
                        evidence_root=temp_dir,
                        repo_root=REPO_ROOT,
                        workspace_root=REPO_ROOT.parent,
                    )

    def test_failed_validation_projects_blocked_without_raising(self) -> None:
        with tempfile.TemporaryDirectory(prefix="wgcf-activity-") as temp_dir:
            with (
                patch(
                    "control_fabric_core.orchestration_activities."
                    "run_catalog_operator_validation_check",
                    return_value=validation_result("failure"),
                ),
                patch(
                    "control_fabric_core.orchestration_activities."
                    "run_operator_readiness_evaluation",
                    return_value=readiness_result(),
                ),
            ):
                result = execute_validation_readiness_activity(
                    valid_request(),
                    activity_context=ValidationReadinessActivityContext(
                        activity_id="activity:blocked",
                        attempt=1,
                        worker_id="wgcf-activity-worker",
                        workflow_id="workflow:validation-readiness-698",
                    ),
                    evidence_root=temp_dir,
                    repo_root=REPO_ROOT,
                    workspace_root=REPO_ROOT.parent,
                )

        self.assertEqual(result["status_code"], "blocked")
        self.assertFalse(result["bounded_decision"]["ready"])
        self.assertEqual(
            result["bounded_decision"]["validation_outcome"],
            "failure",
        )

    def test_activity_schemas_are_strict_and_do_not_admit_paths(self) -> None:
        request_schema = json.loads(
            (
                REPO_ROOT
                / "schemas/validation-readiness-activity-request.schema.json"
            ).read_text(encoding="utf-8"),
        )
        result_schema = json.loads(
            (
                REPO_ROOT
                / "schemas/validation-readiness-activity-result.schema.json"
            ).read_text(encoding="utf-8"),
        )

        self.assertFalse(request_schema["additionalProperties"])
        self.assertFalse(result_schema["additionalProperties"])
        self.assertNotIn("path", request_schema["properties"])
        self.assertNotIn("path", result_schema["properties"])
        self.assertNotIn("raw_output", result_schema["properties"])
