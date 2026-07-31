from __future__ import annotations

import asyncio
import json
import signal
import sys
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
                "wgcf_worker.activities._run_owner_execution",
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
                "wgcf_worker.activities._run_owner_execution",
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
                "wgcf_worker.activities._run_owner_execution",
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
                "wgcf_worker.activities._run_owner_execution",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await validation_readiness_activity(valid_request())

    async def test_owner_process_heartbeats_until_bounded_result(self) -> None:
        process = _FakeOwnerProcess()
        heartbeat = Mock()
        with (
            patch(
                "wgcf_worker.activities.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=process),
            ),
            patch("wgcf_worker.activities.activity.heartbeat", heartbeat),
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
            await _stop_owner_process(process, grace_seconds=0.001)

        signal_group.assert_called_once_with(process.pid, signal.SIGKILL)


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
