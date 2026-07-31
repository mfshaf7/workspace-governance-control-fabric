"""Temporal adapters for WGCF-owned orchestration activities."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from control_fabric_core.orchestration_activities import (
    VALIDATION_READINESS_ACTIVITY_NAME,
    ValidationReadinessFailureClassification,
    classify_validation_readiness_exception,
)


WORKER_ID_ENV = "WGCF_TEMPORAL_WORKER_ID"
OWNER_EXECUTION_TIMEOUT_SECONDS = 240.0
OWNER_HEARTBEAT_INTERVAL_SECONDS = 2.0
OWNER_TERMINATION_GRACE_SECONDS = 5.0
_OWNER_PROTOCOL_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class _OwnerExecutionFailure(Exception):
    error_type: str
    public_message: str
    retryable: bool


@activity.defn(name=VALIDATION_READINESS_ACTIVITY_NAME)
async def validation_readiness_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Run bounded owner work with heartbeat and process-level cancellation."""

    info = activity.info()
    envelope = {
        "schema_version": _OWNER_PROTOCOL_SCHEMA_VERSION,
        "payload": payload,
        "activity_context": {
            "activity_id": info.activity_id,
            "attempt": info.attempt,
            "worker_id": os.environ.get(WORKER_ID_ENV, "wgcf-activity-worker"),
            "workflow_id": info.workflow_id,
        },
    }
    try:
        return await _run_owner_execution(envelope)
    except asyncio.CancelledError:
        raise
    except _OwnerExecutionFailure as failure:
        raise _application_error(failure) from None
    except Exception as exc:
        raise _application_error(classify_validation_readiness_exception(exc)) from None


async def _run_owner_execution(
    envelope: dict[str, Any],
    *,
    heartbeat_interval_seconds: float = OWNER_HEARTBEAT_INTERVAL_SECONDS,
    owner_timeout_seconds: float = OWNER_EXECUTION_TIMEOUT_SECONDS,
    termination_grace_seconds: float = OWNER_TERMINATION_GRACE_SECONDS,
) -> dict[str, Any]:
    request = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "wgcf_worker.owner_execution",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    communication = asyncio.create_task(process.communicate(input=request))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + owner_timeout_seconds

    try:
        activity.heartbeat()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await _stop_owner_process(process, termination_grace_seconds)
                await _drain_communication(communication)
                raise TimeoutError("WGCF owner execution exceeded its bounded runtime")
            try:
                stdout, _stderr = await asyncio.wait_for(
                    asyncio.shield(communication),
                    timeout=min(heartbeat_interval_seconds, remaining),
                )
                await _stop_owner_process(process, termination_grace_seconds)
                return _decode_owner_response(stdout, process.returncode)
            except TimeoutError:
                activity.heartbeat()
    except asyncio.CancelledError:
        await _stop_owner_process(process, termination_grace_seconds)
        await _drain_communication(communication)
        raise
    except BaseException:
        if process.returncode is None:
            await _stop_owner_process(process, termination_grace_seconds)
            await _drain_communication(communication)
        raise
    finally:
        if process.returncode is not None:
            await _drain_communication(communication)


async def _stop_owner_process(
    process: asyncio.subprocess.Process,
    grace_seconds: float,
) -> None:
    if process.returncode is not None:
        # The group leader can finish before one of its descendants. The
        # process owns a dedicated session, so finish clearing that group.
        _signal_process_group(process.pid, signal.SIGKILL)
        return

    _signal_process_group(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        _signal_process_group(process.pid, signal.SIGKILL)
        await process.wait()
    else:
        # The group leader can exit before a command it launched. A final group
        # kill ensures no descendant keeps mutating evidence after acknowledgement.
        _signal_process_group(process.pid, signal.SIGKILL)


def _signal_process_group(process_id: int | None, requested_signal: int) -> None:
    if process_id is None:
        return
    try:
        os.killpg(process_id, requested_signal)
    except ProcessLookupError:
        pass


async def _drain_communication(
    communication: asyncio.Task[tuple[bytes, bytes]],
) -> None:
    try:
        await asyncio.shield(communication)
    except (asyncio.CancelledError, Exception):
        pass


def _decode_owner_response(stdout: bytes, returncode: int | None) -> dict[str, Any]:
    if returncode != 0:
        raise RuntimeError("WGCF owner process exited without a bounded result")
    response = json.loads(stdout.decode("utf-8"))
    if not isinstance(response, dict) or response.get("schema_version") != 1:
        raise RuntimeError("WGCF owner process returned an invalid envelope")
    if response.get("status") == "completed" and isinstance(response.get("result"), dict):
        return response["result"]
    failure = response.get("failure")
    if response.get("status") != "failed" or not isinstance(failure, dict):
        raise RuntimeError("WGCF owner process returned an invalid outcome")
    if set(failure) != {"error_type", "public_message", "retryable"}:
        raise RuntimeError("WGCF owner process returned an invalid failure")
    if not isinstance(failure["retryable"], bool):
        raise RuntimeError("WGCF owner process returned an invalid failure")
    if not all(isinstance(failure[field], str) for field in ("error_type", "public_message")):
        raise RuntimeError("WGCF owner process returned an invalid failure")
    raise _OwnerExecutionFailure(
        error_type=failure["error_type"],
        public_message=failure["public_message"],
        retryable=failure["retryable"],
    )


def _application_error(
    failure: ValidationReadinessFailureClassification | _OwnerExecutionFailure,
) -> ApplicationError:
    return ApplicationError(
        failure.public_message,
        type=failure.error_type,
        non_retryable=not failure.retryable,
    )
