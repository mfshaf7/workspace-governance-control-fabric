from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from temporalio.exceptions import ApplicationError


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))
sys.path.insert(0, str(REPO_ROOT / "apps/worker/src"))

from control_fabric_core.orchestration_activities import (
    ValidationReadinessContractError,
)
from wgcf_worker.activities import (
    _OwnerExecutionFailure,
    _process_group_exists,
    _run_fenced_owner_execution,
    _run_owner_execution,
    _stop_owner_process,
    validation_readiness_activity,
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
        "causation_id": "causation:delivery-719",
        "idempotency_key": "idempotency:delivery-698-proof-01",
        "caller_id": "operator-orchestration-service",
        "operator_id": "operator:test",
    }


def activity_info() -> SimpleNamespace:
    return SimpleNamespace(
        activity_id="activity:validation-readiness",
        attempt=1,
        workflow_id="workflow:validation-readiness-698",
    )


class WorkerActivityTests(IsolatedAsyncioTestCase):
    async def test_contract_rejection_is_non_retryable_and_suppresses_detail(
        self,
    ) -> None:
        with (
            patch("wgcf_worker.activities.activity.info", return_value=activity_info()),
            patch(
                "wgcf_worker.activities._run_fenced_owner_execution",
                new=AsyncMock(
                    side_effect=ValidationReadinessContractError(
                        "raw contract detail",
                    ),
                ),
            ),
        ):
            with self.assertRaises(ApplicationError) as raised:
                await validation_readiness_activity(valid_request())

        self.assertEqual(raised.exception.type, "WGCF_CONTRACT_REJECTED")
        self.assertTrue(raised.exception.non_retryable)
        self.assertNotIn("raw contract detail", str(raised.exception))

    async def test_unavailable_failure_is_retryable_and_suppresses_detail(
        self,
    ) -> None:
        with (
            patch("wgcf_worker.activities.activity.info", return_value=activity_info()),
            patch(
                "wgcf_worker.activities._run_fenced_owner_execution",
                new=AsyncMock(side_effect=OSError("raw filesystem detail")),
            ),
        ):
            with self.assertRaises(ApplicationError) as raised:
                await validation_readiness_activity(valid_request())

        self.assertEqual(raised.exception.type, "WGCF_ACTIVITY_UNAVAILABLE")
        self.assertFalse(raised.exception.non_retryable)
        self.assertNotIn("raw filesystem detail", str(raised.exception))

    async def test_unexpected_failure_is_retryable_and_suppresses_detail(
        self,
    ) -> None:
        with (
            patch("wgcf_worker.activities.activity.info", return_value=activity_info()),
            patch(
                "wgcf_worker.activities._run_fenced_owner_execution",
                new=AsyncMock(side_effect=RuntimeError("raw internal detail")),
            ),
        ):
            with self.assertRaises(ApplicationError) as raised:
                await validation_readiness_activity(valid_request())

        self.assertEqual(raised.exception.type, "WGCF_ACTIVITY_RETRYABLE")
        self.assertFalse(raised.exception.non_retryable)
        self.assertNotIn("raw internal detail", str(raised.exception))

    async def test_temporal_cancellation_is_not_repackaged(self) -> None:
        with (
            patch("wgcf_worker.activities.activity.info", return_value=activity_info()),
            patch(
                "wgcf_worker.activities._run_fenced_owner_execution",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await validation_readiness_activity(valid_request())

    async def test_owner_process_heartbeats_until_bounded_result(self) -> None:
        process = _FakeOwnerProcess()
        heartbeat = Mock()
        stop_owner = AsyncMock()
        with (
            patch(
                "wgcf_worker.activities.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            patch("wgcf_worker.activities.activity.heartbeat", heartbeat),
            patch(
                "wgcf_worker.activities._stop_owner_process",
                new=stop_owner,
            ),
        ):
            execution = asyncio.create_task(
                _run_owner_execution(
                    {"schema_version": 1},
                    heartbeat_interval_seconds=0.001,
                    owner_timeout_seconds=1,
                ),
            )
            await process.communicating.wait()
            await asyncio.sleep(0.005)
            process.complete({"ok": True})

            self.assertEqual(await execution, {"ok": True})

        self.assertGreaterEqual(heartbeat.call_count, 2)
        stop_owner.assert_awaited_once_with(process, 5.0, 5.0)

    async def test_owner_result_is_rejected_when_group_exit_is_unconfirmed(self) -> None:
        process = _FakeOwnerProcess()
        process.complete({"ok": True})
        with (
            patch(
                "wgcf_worker.activities.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            patch("wgcf_worker.activities.activity.heartbeat"),
            patch(
                "wgcf_worker.activities._stop_owner_process",
                new=AsyncMock(return_value=False),
            ),
        ):
            with self.assertRaisesRegex(OSError, "termination was not confirmed"):
                await _run_owner_execution(
                    {"schema_version": 1},
                    owner_timeout_seconds=1,
                )

    async def test_owner_deadline_starts_before_process_spawn(self) -> None:
        async def delayed_spawn(*_args: object, **_kwargs: object) -> _FakeOwnerProcess:
            await asyncio.sleep(0.01)
            return _FakeOwnerProcess()

        with (
            patch(
                "wgcf_worker.activities.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=delayed_spawn),
            ),
            patch("wgcf_worker.activities.activity.heartbeat"),
        ):
            with self.assertRaises(TimeoutError):
                await _run_owner_execution(
                    {"schema_version": 1},
                    owner_timeout_seconds=0.001,
                )

    async def test_real_owner_process_returns_bounded_failure(self) -> None:
        envelope = {
            "schema_version": 1,
            "payload": {"schema_version": 1},
            "activity_context": {
                "activity_id": "activity:validation-readiness",
                "attempt": 1,
                "worker_id": "wgcf-worker:test",
                "workflow_id": "workflow:validation-readiness-698",
            },
        }
        with patch("wgcf_worker.activities.activity.heartbeat"):
            with self.assertRaises(_OwnerExecutionFailure) as raised:
                await _run_owner_execution(
                    envelope,
                    heartbeat_interval_seconds=0.1,
                    owner_timeout_seconds=5,
                )

        self.assertEqual(raised.exception.error_type, "WGCF_CONTRACT_REJECTED")

    async def test_temporal_cancellation_waits_for_owner_process_to_stop(
        self,
    ) -> None:
        process = _FakeOwnerProcess()
        allow_stop = asyncio.Event()
        stop_started = asyncio.Event()
        stopped = asyncio.Event()

        async def stop_owner(*_args: object) -> None:
            self.assertFalse(stopped.is_set())
            stop_started.set()
            await allow_stop.wait()
            process.complete({"ignored": True}, returncode=-15)
            stopped.set()

        with (
            patch(
                "wgcf_worker.activities.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            patch("wgcf_worker.activities.activity.heartbeat"),
            patch(
                "wgcf_worker.activities._stop_owner_process",
                new=AsyncMock(side_effect=stop_owner),
            ),
        ):
            activity_task = asyncio.create_task(
                _run_owner_execution(
                    {"schema_version": 1},
                    heartbeat_interval_seconds=1,
                    owner_timeout_seconds=10,
                ),
            )
            await process.communicating.wait()
            activity_task.cancel()
            await stop_started.wait()

            self.assertFalse(activity_task.done())
            self.assertFalse(stopped.is_set())
            allow_stop.set()

            with self.assertRaises(asyncio.CancelledError):
                await activity_task

        self.assertTrue(stopped.is_set())

    async def test_owner_timeout_stops_process_before_returning(self) -> None:
        process = _FakeOwnerProcess()
        stopped = asyncio.Event()

        async def stop_owner(*_args: object) -> None:
            process.complete({"ignored": True}, returncode=-15)
            stopped.set()

        with (
            patch(
                "wgcf_worker.activities.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            patch("wgcf_worker.activities.activity.heartbeat"),
            patch(
                "wgcf_worker.activities._stop_owner_process",
                new=AsyncMock(side_effect=stop_owner),
            ),
        ):
            with self.assertRaises(TimeoutError):
                await _run_owner_execution(
                    {"schema_version": 1},
                    heartbeat_interval_seconds=0.001,
                    owner_timeout_seconds=0.002,
                )

        self.assertTrue(stopped.is_set())

    async def test_completed_group_leader_still_clears_descendants(self) -> None:
        process = _FakeOwnerProcess()
        process.complete({}, returncode=0)

        with patch("wgcf_worker.activities._signal_process_group") as signal_group:
            fenced = await _stop_owner_process(
                process,
                grace_seconds=0.001,
                fence_confirmation_seconds=0.001,
            )

        self.assertTrue(fenced)
        signal_group.assert_called_once_with(process.pid, signal.SIGKILL)

    async def test_group_exit_confirmation_is_bounded(self) -> None:
        process = _FakeOwnerProcess()
        process.complete({}, returncode=0)

        with (
            patch("wgcf_worker.activities._signal_process_group"),
            patch(
                "wgcf_worker.activities._process_group_exists",
                return_value=True,
            ),
        ):
            fenced = await _stop_owner_process(
                process,
                grace_seconds=0.001,
                fence_confirmation_seconds=0.002,
            )

        self.assertFalse(fenced)

    async def test_real_completed_leader_descendant_is_confirmed_gone(self) -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            (
                "import subprocess, sys; "
                "subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(30)'], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL)"
            ),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        await process.wait()
        try:
            self.assertTrue(_process_group_exists(process.pid))
            self.assertTrue(
                await _stop_owner_process(
                    process,
                    grace_seconds=0.1,
                    fence_confirmation_seconds=1,
                ),
            )
            self.assertFalse(_process_group_exists(process.pid))
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def test_fenced_wrapper_uses_attempt_staging_before_commit(self) -> None:
        result = {"schema_version": 1, "status_code": "ready"}
        observed_root: Path | None = None

        async def owner_run(
            _envelope: dict[str, object],
            *,
            owner_environment: dict[str, str],
        ) -> dict[str, object]:
            nonlocal observed_root
            observed_root = Path(
                owner_environment["WGCF_ORCHESTRATION_EVIDENCE_ROOT"],
            )
            (observed_root / "marker").write_text("staged", encoding="utf-8")
            return result

        with tempfile.TemporaryDirectory(prefix="wgcf-worker-fence-") as temp_dir:
            with (
                patch.dict(
                    os.environ,
                    {"WGCF_ORCHESTRATION_EVIDENCE_ROOT": temp_dir},
                ),
                patch(
                    "wgcf_worker.activities.load_committed_validation_readiness_result",
                    return_value=None,
                ),
                patch(
                    "wgcf_worker.activities._run_owner_execution",
                    new=AsyncMock(side_effect=owner_run),
                ),
                patch(
                    "wgcf_worker.activities.commit_validation_readiness_staging_result",
                    return_value=result,
                ) as commit_result,
            ):
                actual = await _run_fenced_owner_execution(
                    {
                        "payload": valid_request(),
                        "activity_context": {
                            "workflow_id": "workflow:validation-readiness-698",
                        },
                    },
                )

            self.assertEqual(actual, result)
            self.assertIsNotNone(observed_root)
            assert observed_root is not None
            self.assertEqual(observed_root.parent, Path(temp_dir) / "staging")
            self.assertFalse(observed_root.exists())
            commit_result.assert_called_once()

    async def test_committed_result_cannot_bypass_workflow_binding(self) -> None:
        with patch(
            "wgcf_worker.activities.load_committed_validation_readiness_result",
            return_value={"schema_version": 1, "status_code": "ready"},
        ) as load_result:
            with self.assertRaisesRegex(
                ValidationReadinessContractError,
                "must match the Temporal execution context",
            ):
                await _run_fenced_owner_execution(
                    {
                        "payload": valid_request(),
                        "activity_context": {
                            "workflow_id": "workflow:different",
                        },
                    },
                )

        load_result.assert_not_called()

    async def test_failed_owner_attempt_is_quarantined_without_commit(self) -> None:
        async def owner_run(
            _envelope: dict[str, object],
            *,
            owner_environment: dict[str, str],
        ) -> dict[str, object]:
            staging_root = Path(
                owner_environment["WGCF_ORCHESTRATION_EVIDENCE_ROOT"],
            )
            (staging_root / "marker").write_text("uncommitted", encoding="utf-8")
            raise OSError("termination fence not confirmed")

        with tempfile.TemporaryDirectory(prefix="wgcf-worker-fence-") as temp_dir:
            evidence_root = Path(temp_dir)
            with (
                patch.dict(
                    os.environ,
                    {"WGCF_ORCHESTRATION_EVIDENCE_ROOT": temp_dir},
                ),
                patch(
                    "wgcf_worker.activities.load_committed_validation_readiness_result",
                    return_value=None,
                ),
                patch(
                    "wgcf_worker.activities._run_owner_execution",
                    new=AsyncMock(side_effect=owner_run),
                ),
                patch(
                    "wgcf_worker.activities.commit_validation_readiness_staging_result",
                ) as commit_result,
            ):
                with self.assertRaisesRegex(OSError, "fence not confirmed"):
                    await _run_fenced_owner_execution(
                        {
                            "payload": valid_request(),
                            "activity_context": {
                                "workflow_id": "workflow:validation-readiness-698",
                            },
                        },
                    )

            commit_result.assert_not_called()
            self.assertFalse((evidence_root / "committed").exists())
            quarantined = list((evidence_root / "quarantine").glob("*/marker"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "uncommitted")


class _FakeOwnerProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.communicating = asyncio.Event()
        self._completed = asyncio.Event()
        self._stdout = b""

    async def communicate(self, *, input: bytes) -> tuple[bytes, bytes]:
        self.input = input
        self.communicating.set()
        await self._completed.wait()
        return self._stdout, b""

    async def wait(self) -> int:
        await self._completed.wait()
        return self.returncode or 0

    def complete(self, result: dict[str, object], *, returncode: int = 0) -> None:
        self.returncode = returncode
        self._stdout = json.dumps(
            {
                "schema_version": 1,
                "status": "completed",
                "result": result,
            },
        ).encode("utf-8")
        self._completed.set()
