"""Temporal adapters for WGCF-owned orchestration activities."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from control_fabric_core.orchestration_activities import (
    VALIDATION_READINESS_ACTIVITY_NAME,
    ValidationReadinessActivityRequest,
    ValidationReadinessContractError,
    ValidationReadinessFailureClassification,
    classify_validation_readiness_exception,
    commit_validation_readiness_staging_result,
    load_committed_validation_readiness_result,
)


WORKER_ID_ENV = "WGCF_TEMPORAL_WORKER_ID"
EVIDENCE_ROOT_ENV = "WGCF_ORCHESTRATION_EVIDENCE_ROOT"
OWNER_EXECUTION_TIMEOUT_SECONDS = 240.0
OWNER_HEARTBEAT_INTERVAL_SECONDS = 2.0
OWNER_TERMINATION_GRACE_SECONDS = 5.0
OWNER_FENCE_CONFIRMATION_SECONDS = 5.0
OWNER_COMMUNICATION_DRAIN_SECONDS = 1.0
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
        return await _run_fenced_owner_execution(envelope)
    except asyncio.CancelledError:
        raise
    except _OwnerExecutionFailure as failure:
        raise _application_error(failure) from None
    except Exception as exc:
        raise _application_error(classify_validation_readiness_exception(exc)) from None


async def _run_fenced_owner_execution(
    envelope: dict[str, Any],
) -> dict[str, Any]:
    payload = envelope["payload"]
    request = ValidationReadinessActivityRequest.from_payload(payload)
    if request.workflow_id != envelope["activity_context"]["workflow_id"]:
        raise ValidationReadinessContractError(
            "workflow_id must match the Temporal execution context",
        )
    evidence_root = Path(
        os.environ.get(
            EVIDENCE_ROOT_ENV,
            "/var/lib/wgcf/orchestration/validation-readiness",
        ),
    ).resolve()
    committed = load_committed_validation_readiness_result(
        payload,
        evidence_root=evidence_root,
    )
    if committed is not None:
        return committed

    staging_parent = evidence_root / "staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(prefix="owner-attempt-", dir=staging_parent),
    ).resolve()
    owner_environment = dict(os.environ)
    owner_environment[EVIDENCE_ROOT_ENV] = str(staging_root)
    committed_result = False
    try:
        result = await _run_owner_execution(
            envelope,
            owner_environment=owner_environment,
        )
        canonical_result = commit_validation_readiness_staging_result(
            payload,
            evidence_root=evidence_root,
            staging_root=staging_root,
            expected_result=result,
        )
        if staging_root.exists():
            shutil.rmtree(staging_root)
        committed_result = True
        return canonical_result
    finally:
        if not committed_result:
            _quarantine_staging_root(evidence_root, staging_root)


async def _run_owner_execution(
    envelope: dict[str, Any],
    *,
    heartbeat_interval_seconds: float = OWNER_HEARTBEAT_INTERVAL_SECONDS,
    owner_timeout_seconds: float = OWNER_EXECUTION_TIMEOUT_SECONDS,
    termination_grace_seconds: float = OWNER_TERMINATION_GRACE_SECONDS,
    fence_confirmation_seconds: float = OWNER_FENCE_CONFIRMATION_SECONDS,
    owner_environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + owner_timeout_seconds
    process: asyncio.subprocess.Process | None = None
    communication: asyncio.Task[tuple[bytes, bytes]] | None = None
    group_fenced = False
    stop_attempted = False

    try:
        activity.heartbeat()
        process = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "wgcf_worker.owner_execution",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env=owner_environment,
            ),
            timeout=max(deadline - loop.time(), 0.001),
        )
        communication = asyncio.create_task(process.communicate(input=request))
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                stop_attempted = True
                group_fenced = await _stop_owner_process(
                    process,
                    termination_grace_seconds,
                    fence_confirmation_seconds,
                )
                await _drain_communication(communication)
                raise TimeoutError("WGCF owner execution exceeded its bounded runtime")
            try:
                stdout, _stderr = await asyncio.wait_for(
                    asyncio.shield(communication),
                    timeout=min(heartbeat_interval_seconds, remaining),
                )
                stop_attempted = True
                group_fenced = await _stop_owner_process(
                    process,
                    termination_grace_seconds,
                    fence_confirmation_seconds,
                )
                if not group_fenced:
                    raise OSError(
                        "WGCF owner process group termination was not confirmed",
                    )
                return _decode_owner_response(stdout, process.returncode)
            except TimeoutError:
                activity.heartbeat()
    except asyncio.CancelledError:
        if process is not None and not stop_attempted:
            stop_attempted = True
            group_fenced = await _stop_owner_process(
                process,
                termination_grace_seconds,
                fence_confirmation_seconds,
            )
        if communication is not None:
            await _drain_communication(communication)
        raise
    except BaseException:
        if process is not None and not stop_attempted:
            stop_attempted = True
            group_fenced = await _stop_owner_process(
                process,
                termination_grace_seconds,
                fence_confirmation_seconds,
            )
        if communication is not None:
            await _drain_communication(communication)
        raise
    finally:
        if communication is not None:
            await _drain_communication(communication)


async def _stop_owner_process(
    process: asyncio.subprocess.Process,
    grace_seconds: float,
    fence_confirmation_seconds: float,
) -> bool:
    if process.returncode is not None:
        _signal_process_group(process.pid, signal.SIGKILL)
    else:
        _signal_process_group(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except TimeoutError:
            _signal_process_group(process.pid, signal.SIGKILL)
        else:
            # The leader can exit before a descendant. Clear the complete
            # dedicated session before attempting to acknowledge the result.
            _signal_process_group(process.pid, signal.SIGKILL)
    return await _wait_for_process_group_exit(
        process.pid,
        timeout_seconds=fence_confirmation_seconds,
    )


async def _wait_for_process_group_exit(
    process_id: int | None,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = 0.05,
) -> bool:
    if process_id is None:
        return True
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while _process_group_exists(process_id):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll_interval_seconds, remaining))
    return True


def _signal_process_group(process_id: int | None, requested_signal: int) -> None:
    if process_id is None:
        return
    try:
        os.killpg(process_id, requested_signal)
    except ProcessLookupError:
        pass


def _process_group_exists(process_id: int) -> bool:
    try:
        os.killpg(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _drain_communication(
    communication: asyncio.Task[tuple[bytes, bytes]],
    *,
    timeout_seconds: float = OWNER_COMMUNICATION_DRAIN_SECONDS,
) -> None:
    try:
        await asyncio.wait_for(
            asyncio.shield(communication),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        communication.cancel()
        await asyncio.gather(communication, return_exceptions=True)
    except (asyncio.CancelledError, Exception):
        pass


def _quarantine_staging_root(evidence_root: Path, staging_root: Path) -> None:
    if not staging_root.exists():
        return
    quarantine_root = evidence_root / "quarantine"
    quarantine_root.mkdir(parents=True, exist_ok=True)
    target = quarantine_root / staging_root.name
    try:
        staging_root.rename(target)
    except OSError:
        # The path remains non-canonical under staging if a late process still
        # holds it. Only committed/ grants evidence authority.
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
