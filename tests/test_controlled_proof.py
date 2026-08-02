from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

import control_fabric_core.controlled_proof as controlled_proof
from control_fabric_core.controlled_proof import (
    CONTROLLED_PROOF_ACTIVITY_TASK_QUEUE,
    CONTROLLED_PROOF_WORKER_ID,
    ControlledProofAuthorizationError,
    ControlledProofContextMismatch,
    ControlledProofIdentityDenied,
    authorize_controlled_proof_activity_request,
    bind_controlled_proof_request,
    commit_controlled_proof_owner_receipt,
    load_controlled_proof_owner_context,
    load_controlled_proof_owner_receipt,
    owner_result_for_activity_result,
)
from tests.controlled_proof_fixtures import (
    ACTIVITY_STARTED_AT,
    WGCF_REVISION,
    valid_controlled_request,
    valid_owner_context,
    write_owner_context,
)


class ControlledProofContractTests(TestCase):
    def test_owner_context_binds_consumed_authorization_and_exact_scenarios(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path, digest = write_owner_context(Path(temp_dir))

            context = load_controlled_proof_owner_context(
                path,
                expected_digest=digest,
            )

        self.assertEqual(context.owner_context_digest, digest)
        self.assertEqual(context.worker_identity, CONTROLLED_PROOF_WORKER_ID)
        self.assertEqual(
            context.activity_task_queue,
            CONTROLLED_PROOF_ACTIVITY_TASK_QUEUE,
        )
        self.assertEqual(len(context.scenario_executions), 11)
        self.assertEqual(
            context.consumption_receipt_ref,
            "platform-engineering://controlled-proof/consumption-698-1",
        )

    def test_owner_context_rejects_digest_drift_and_writable_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path, digest = write_owner_context(Path(temp_dir))
            with self.assertRaises(ControlledProofContextMismatch):
                load_controlled_proof_owner_context(
                    path,
                    expected_digest=f"sha256:{'f' * 64}",
                )

            path.chmod(0o620)
            with self.assertRaises(ControlledProofAuthorizationError):
                load_controlled_proof_owner_context(path, expected_digest=digest)

    def test_owner_context_rejects_scenario_order_and_restore_owner_drift(self) -> None:
        for mutation in ("order", "restore-owner"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp_dir:
                record = valid_owner_context()
                scenarios = record["commissioning_session"]["scenario_executions"]
                if mutation == "order":
                    scenarios[0], scenarios[1] = scenarios[1], scenarios[0]
                else:
                    scenarios[-1]["required_receipt_owners"].append(
                        "workspace-governance-control-fabric",
                    )
                path = Path(temp_dir) / "context.json"
                raw = json.dumps(record, sort_keys=True) + "\n"
                path.write_text(raw, encoding="utf-8")
                path.chmod(0o600)
                digest = sha256(raw.encode()).hexdigest()

                with self.assertRaises(
                    (
                        ControlledProofContextMismatch,
                        ControlledProofAuthorizationError,
                    ),
                ):
                    load_controlled_proof_owner_context(
                        path,
                        expected_digest=f"sha256:{digest}",
                    )

    def test_activity_request_is_bound_to_context_runtime_and_source(self) -> None:
        request, context = self._authorized()

        self.assertEqual(request.scenario.scenario_id, "nominal-completion")
        self.assertEqual(
            request.normal_activity_payload["caller_id"],
            "operator-orchestration-service",
        )
        self.assertNotIn(
            "controlled_proof_execution",
            request.normal_activity_payload,
        )
        self.assertEqual(request.owner_context, context)

    def test_activity_request_rejects_cross_boundary_runtime_bindings(self) -> None:
        request = valid_controlled_request()
        with tempfile.TemporaryDirectory() as temp_dir:
            path, digest = write_owner_context(Path(temp_dir))
            context = load_controlled_proof_owner_context(
                path,
                expected_digest=digest,
            )
            cases = {
                "identity": {"worker_identity": "wrong-worker"},
                "namespace": {"temporal_namespace": "wrong-namespace"},
                "queue": {"task_queue": "wgcf.validation-readiness.v1"},
                "source": {"source_revision": "e" * 40},
            }
            for label, override in cases.items():
                arguments = self._authorization_arguments()
                arguments.update(override)
                with self.subTest(label=label), self.assertRaises(
                    (ControlledProofContextMismatch, ControlledProofIdentityDenied),
                ):
                    authorize_controlled_proof_activity_request(
                        request,
                        owner_context=context,
                        **arguments,
                    )

    def test_activity_request_rejects_expired_start_and_missing_owner(self) -> None:
        request = valid_controlled_request()
        with tempfile.TemporaryDirectory() as temp_dir:
            path, digest = write_owner_context(Path(temp_dir))
            context = load_controlled_proof_owner_context(
                path,
                expected_digest=digest,
            )
            arguments = self._authorization_arguments()
            arguments["started_at"] = datetime(2100, 1, 1, tzinfo=timezone.utc)
            with self.assertRaises(ControlledProofAuthorizationError):
                authorize_controlled_proof_activity_request(
                    request,
                    owner_context=context,
                    **arguments,
                )

            request["controlled_proof_execution"]["required_receipt_owners"] = [
                "platform-engineering",
            ]
            arguments["started_at"] = ACTIVITY_STARTED_AT
            with self.assertRaises(ControlledProofAuthorizationError):
                authorize_controlled_proof_activity_request(
                    request,
                    owner_context=context,
                    **arguments,
                )

    def test_idempotency_binding_rejects_cross_request_reuse(self) -> None:
        first, _context = self._authorized()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_path = bind_controlled_proof_request(first, evidence_root=root)
            self.assertEqual(
                bind_controlled_proof_request(first, evidence_root=root),
                first_path,
            )

            payload = valid_controlled_request()
            payload["correlation_id"] = "controlled-proof-session:other"
            second, _ = self._authorized(payload=payload)
            with self.assertRaises(ControlledProofContextMismatch):
                bind_controlled_proof_request(second, evidence_root=root)

    def test_scenario_binding_rejects_a_different_workflow_run(self) -> None:
        first, _context = self._authorized()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bind_controlled_proof_request(first, evidence_root=root)

            payload = valid_controlled_request(
                idempotency_key="activity:controlled-proof:scenario-01:attempt-2",
            )
            payload["run_id"] = "temporal-execution:controlled-proof-replay"
            arguments = self._authorization_arguments()
            arguments["workflow_run_id"] = payload["run_id"]
            second, _ = self._authorized(
                payload=payload,
                arguments=arguments,
            )
            with self.assertRaises(ControlledProofContextMismatch):
                bind_controlled_proof_request(second, evidence_root=root)

    def test_owner_receipt_is_bounded_deterministic_and_context_bound(self) -> None:
        request, _context = self._authorized()
        activity_result = {
            "status_code": "ready",
            "receipt_ref": {
                "receipt_id": "receipt:wgcf:controlled-proof:1",
                "digest": f"sha256:{'b' * 64}",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            receipt = commit_controlled_proof_owner_receipt(
                request,
                evidence_root=root,
                owner_result="passed",
                observation_kind="activity-result-recorded",
                process_group_fenced=True,
                activity_result=activity_result,
                recorded_at=datetime(2026, 8, 2, 0, 4, tzinfo=timezone.utc),
            )
            replay = commit_controlled_proof_owner_receipt(
                request,
                evidence_root=root,
                owner_result="passed",
                observation_kind="activity-result-recorded",
                process_group_fenced=True,
                activity_result=activity_result,
            )

            self.assertEqual(replay, receipt)
            self.assertEqual(receipt["owner_repo"], "workspace-governance-control-fabric")
            self.assertEqual(receipt["owner_execution"]["execution_type"], "activity")
            self.assertGreaterEqual(len(receipt["evidence_refs"]), 8)
            self.assertNotIn("raw_output", json.dumps(receipt))
            self.assertEqual(
                len(list((root / "activity-results").glob("*.json"))),
                1,
            )
            self.assertEqual(
                load_controlled_proof_owner_receipt(request, evidence_root=root),
                receipt,
            )
            with self.assertRaises(ControlledProofContextMismatch):
                commit_controlled_proof_owner_receipt(
                    request,
                    evidence_root=root,
                    owner_result="failed",
                    observation_kind="activity-result-recorded",
                    process_group_fenced=True,
                )

    def test_owner_receipt_replay_rejects_different_activity_evidence(self) -> None:
        request, _context = self._authorized()
        activity_result = {
            "status_code": "ready",
            "receipt_ref": {
                "receipt_id": "receipt:wgcf:controlled-proof:1",
                "digest": f"sha256:{'b' * 64}",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            commit_controlled_proof_owner_receipt(
                request,
                evidence_root=temp_dir,
                owner_result="passed",
                observation_kind="activity-result-recorded",
                process_group_fenced=True,
                activity_result=activity_result,
                recorded_at=datetime(2026, 8, 2, 0, 4, tzinfo=timezone.utc),
            )
            changed_result = json.loads(json.dumps(activity_result))
            changed_result["receipt_ref"]["digest"] = f"sha256:{'c' * 64}"
            with self.assertRaises(ControlledProofContextMismatch):
                commit_controlled_proof_owner_receipt(
                    request,
                    evidence_root=temp_dir,
                    owner_result="passed",
                    observation_kind="activity-result-recorded",
                    process_group_fenced=True,
                    activity_result=changed_result,
                )

    def test_owner_receipt_replays_across_a_later_temporal_attempt(self) -> None:
        request, _context = self._authorized()
        activity_result = {
            "status_code": "ready",
            "receipt_ref": {
                "receipt_id": "receipt:wgcf:controlled-proof:1",
                "digest": f"sha256:{'b' * 64}",
            },
        }
        later_arguments = self._authorization_arguments()
        later_arguments["attempt"] = 2
        later_arguments["started_at"] = ACTIVITY_STARTED_AT + timedelta(minutes=5)
        retry_request, _ = self._authorized(arguments=later_arguments)

        with tempfile.TemporaryDirectory() as temp_dir:
            receipt = commit_controlled_proof_owner_receipt(
                request,
                evidence_root=temp_dir,
                owner_result="passed",
                observation_kind="activity-result-recorded",
                process_group_fenced=True,
                activity_result=activity_result,
                recorded_at=datetime(2026, 8, 2, 0, 4, tzinfo=timezone.utc),
            )

            replay = commit_controlled_proof_owner_receipt(
                retry_request,
                evidence_root=temp_dir,
                owner_result="passed",
                observation_kind="activity-result-recorded",
                process_group_fenced=True,
                activity_result=activity_result,
            )

            self.assertEqual(replay, receipt)

    def test_owner_receipt_recovers_after_observation_commit_interruption(
        self,
    ) -> None:
        request, _context = self._authorized()
        activity_result = {
            "status_code": "ready",
            "receipt_ref": {
                "receipt_id": "receipt:wgcf:controlled-proof:1",
                "digest": f"sha256:{'b' * 64}",
            },
        }
        original_write = controlled_proof._write_json_atomic

        def interrupt_receipt(path, record):
            if path.parent.name == "receipts":
                raise OSError("simulated interruption before receipt commit")
            original_write(path, record)

        later_arguments = self._authorization_arguments()
        later_arguments["attempt"] = 2
        later_arguments["started_at"] = ACTIVITY_STARTED_AT + timedelta(minutes=5)
        retry_request, _ = self._authorized(arguments=later_arguments)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                controlled_proof,
                "_write_json_atomic",
                side_effect=interrupt_receipt,
            ), self.assertRaises(OSError):
                commit_controlled_proof_owner_receipt(
                    request,
                    evidence_root=temp_dir,
                    owner_result="passed",
                    observation_kind="activity-result-recorded",
                    process_group_fenced=True,
                    activity_result=activity_result,
                    recorded_at=datetime(2026, 8, 2, 0, 4, tzinfo=timezone.utc),
                )

            receipt = commit_controlled_proof_owner_receipt(
                retry_request,
                evidence_root=temp_dir,
                owner_result="passed",
                observation_kind="activity-result-recorded",
                process_group_fenced=True,
                activity_result=activity_result,
            )

            self.assertEqual(receipt["recorded_at"], "2026-08-02T00:04:00.000Z")

    def test_owner_receipt_load_rejects_tampered_observation(self) -> None:
        request, _context = self._authorized()
        activity_result = {
            "status_code": "ready",
            "receipt_ref": {
                "receipt_id": "receipt:wgcf:controlled-proof:1",
                "digest": f"sha256:{'b' * 64}",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            commit_controlled_proof_owner_receipt(
                request,
                evidence_root=root,
                owner_result="passed",
                observation_kind="activity-result-recorded",
                process_group_fenced=True,
                activity_result=activity_result,
                recorded_at=datetime(2026, 8, 2, 0, 4, tzinfo=timezone.utc),
            )
            observation_path = next((root / "observations").glob("*.json"))
            observation = json.loads(observation_path.read_text(encoding="utf-8"))
            observation["observation_kind"] = "tampered-observation"
            observation_path.write_text(
                json.dumps(observation, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(ControlledProofContextMismatch):
                load_controlled_proof_owner_receipt(request, evidence_root=root)

    def test_passing_owner_receipt_cannot_be_recorded_after_expiry(self) -> None:
        request, _context = self._authorized()
        activity_result = {
            "status_code": "ready",
            "receipt_ref": {
                "receipt_id": "receipt:wgcf:controlled-proof:1",
                "digest": f"sha256:{'b' * 64}",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ControlledProofAuthorizationError):
                commit_controlled_proof_owner_receipt(
                    request,
                    evidence_root=temp_dir,
                    owner_result="passed",
                    observation_kind="activity-result-recorded",
                    process_group_fenced=True,
                    activity_result=activity_result,
                    recorded_at=datetime(2100, 1, 1, tzinfo=timezone.utc),
                )

    def test_owner_receipt_requires_a_confirmed_process_fence(self) -> None:
        request, _context = self._authorized()
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ControlledProofAuthorizationError):
                commit_controlled_proof_owner_receipt(
                    request,
                    evidence_root=temp_dir,
                    owner_result="passed",
                    observation_kind="activity-result-recorded",
                    process_group_fenced=False,
                )

    def test_negative_scenarios_cannot_pass_from_a_ready_activity_result(self) -> None:
        self.assertEqual(
            owner_result_for_activity_result(
                {"status_code": "ready"},
                scenario_id="payload-boundary",
            ),
            "blocked",
        )

    def _authorized(
        self,
        *,
        payload: dict[str, object] | None = None,
        arguments: dict[str, object] | None = None,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            path, digest = write_owner_context(Path(temp_dir))
            context = load_controlled_proof_owner_context(
                path,
                expected_digest=digest,
            )
        return (
            authorize_controlled_proof_activity_request(
                payload or valid_controlled_request(),
                owner_context=context,
                **(arguments or self._authorization_arguments()),
            ),
            context,
        )

    @staticmethod
    def _authorization_arguments() -> dict[str, object]:
        return {
            "activity_id": "activity:validation-readiness",
            "attempt": 1,
            "worker_identity": CONTROLLED_PROOF_WORKER_ID,
            "temporal_namespace": "default",
            "task_queue": CONTROLLED_PROOF_ACTIVITY_TASK_QUEUE,
            "workflow_id": "workflow:controlled-proof-698-01",
            "workflow_run_id": "temporal-execution:controlled-proof-698-01",
            "source_revision": WGCF_REVISION,
            "started_at": ACTIVITY_STARTED_AT,
        }
