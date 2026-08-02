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
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from control_fabric_core.controlled_proof import (
    CONTROLLED_PROOF_ACTIVITY_TASK_QUEUE,
    AuthorizedControlledProofRequest,
    ControlledProofAuthorizationError,
    ControlledProofIdentityDenied,
    ControlledProofPayloadRejected,
    authorize_controlled_proof_activity_request,
    bind_controlled_proof_request,
    commit_controlled_proof_owner_receipt,
    load_controlled_proof_owner_context,
    owner_result_for_activity_result,
)
from control_fabric_core.orchestration_activities import (
    VALIDATION_READINESS_ACTIVITY_NAME,
    ValidationReadinessActivityRequest,
    ValidationReadinessContractError,
    ValidationReadinessFailureClassification,
    classify_validation_readiness_exception,
    commit_validation_readiness_staging_result,
    load_committed_validation_readiness_result,
)
from control_fabric_core.worker import (
    CONTROLLED_PROOF_CONTEXT_DIGEST_ENV,
    CONTROLLED_PROOF_CONTEXT_PATH_ENV,
    CONTROLLED_PROOF_TEMPORAL_WORKER_ID_ENV,
    controlled_proof_evidence_root,
    controlled_proof_image_source_revision,
)


WORKER_ID_ENV = "WGCF_TEMPORAL_WORKER_ID"
EVIDENCE_ROOT_ENV = "WGCF_ORCHESTRATION_EVIDENCE_ROOT"
ARTIFACT_REFERENCE_ROOT_ENV = "WGCF_ORCHESTRATION_ARTIFACT_REFERENCE_ROOT"
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
    controlled_request: AuthorizedControlledProofRequest | None = None
    try:
        owner_payload = payload
        controlled = info.task_queue == CONTROLLED_PROOF_ACTIVITY_TASK_QUEUE
        has_controlled_envelope = "controlled_proof_execution" in payload
        if controlled != has_controlled_envelope:
            raise ControlledProofAuthorizationError(
                "controlled-proof activity envelope does not match the Temporal task queue",
            )
        worker_id = os.environ.get(
            (
                CONTROLLED_PROOF_TEMPORAL_WORKER_ID_ENV
                if controlled
                else WORKER_ID_ENV
            ),
            (
                "wgcf-controlled-proof-activity-worker"
                if controlled
                else "wgcf-activity-worker"
            ),
        )
        if controlled:
            controlled_request = _authorize_controlled_proof_request(
                payload,
                info=info,
                worker_id=worker_id,
            )
            owner_payload = controlled_request.normal_activity_payload
            ValidationReadinessActivityRequest.from_payload(owner_payload)
            bind_controlled_proof_request(
                controlled_request,
                evidence_root=_controlled_proof_evidence_root(),
            )
            _raise_expected_controlled_proof_boundary(controlled_request)
        envelope = {
            "schema_version": _OWNER_PROTOCOL_SCHEMA_VERSION,
            "payload": owner_payload,
            "activity_context": {
                "activity_id": info.activity_id,
                "attempt": info.attempt,
                "worker_id": worker_id,
                "workflow_id": info.workflow_id,
            },
        }
        result = await _run_fenced_owner_execution(
            envelope,
            evidence_root=(
                _controlled_proof_activity_evidence_root(controlled_request)
                if controlled_request is not None
                else None
            ),
        )
        if controlled_request is not None:
            _assert_controlled_context_remains_current(controlled_request)
            commit_controlled_proof_owner_receipt(
                controlled_request,
                evidence_root=_controlled_proof_evidence_root(),
                owner_result=owner_result_for_activity_result(
                    result,
                    scenario_id=controlled_request.scenario.scenario_id,
                ),
                observation_kind="activity-result-recorded",
                process_group_fenced=True,
                activity_result=result,
            )
        return result
    except asyncio.CancelledError:
        if controlled_request is not None:
            try:
                _commit_controlled_cancellation_receipt(controlled_request)
            except Exception as exc:
                raise _application_error(
                    classify_validation_readiness_exception(exc),
                ) from None
        raise
    except _OwnerExecutionFailure as failure:
        raise _application_error(failure) from None
    except Exception as exc:
        raise _application_error(classify_validation_readiness_exception(exc)) from None


def _authorize_controlled_proof_request(
    payload: dict[str, Any],
    *,
    info: Any,
    worker_id: str,
) -> AuthorizedControlledProofRequest:
    context_path = os.environ.get(CONTROLLED_PROOF_CONTEXT_PATH_ENV, "").strip()
    context_digest = os.environ.get(
        CONTROLLED_PROOF_CONTEXT_DIGEST_ENV,
        "",
    ).strip()
    if not context_path or not context_digest:
        raise ControlledProofAuthorizationError(
            "controlled-proof context path and digest are required",
        )
    source_revision = controlled_proof_image_source_revision()
    owner_context = load_controlled_proof_owner_context(
        context_path,
        expected_digest=context_digest,
    )
    workflow_id = info.workflow_id
    workflow_run_id = info.workflow_run_id
    if not isinstance(workflow_id, str) or not isinstance(workflow_run_id, str):
        raise ControlledProofAuthorizationError(
            "controlled-proof activity requires Temporal workflow identity",
        )
    return authorize_controlled_proof_activity_request(
        payload,
        owner_context=owner_context,
        activity_id=info.activity_id,
        attempt=info.attempt,
        worker_identity=worker_id,
        temporal_namespace=info.namespace,
        task_queue=info.task_queue,
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        source_revision=source_revision,
        started_at=info.started_time,
    )


def _raise_expected_controlled_proof_boundary(
    request: AuthorizedControlledProofRequest,
) -> None:
    scenario = request.scenario.scenario_id
    if scenario == "identity-denial":
        _prove_identity_denial(request)
        _commit_expected_boundary_receipt(request, "identity-denial-enforced")
        raise ControlledProofIdentityDenied(
            "the authorized identity-denial scenario was enforced",
        )
    if scenario == "payload-boundary":
        _prove_payload_boundary(request)
        _commit_expected_boundary_receipt(request, "payload-boundary-enforced")
        raise ControlledProofPayloadRejected(
            "the authorized payload-boundary scenario was enforced",
        )
    if scenario == "unavailable-dependency":
        _commit_expected_boundary_receipt(
            request,
            "dependency-unavailability-simulated",
        )
        raise OSError("authorized controlled-proof dependency unavailability")


def _prove_identity_denial(request: AuthorizedControlledProofRequest) -> None:
    try:
        authorize_controlled_proof_activity_request(
            request.payload,
            owner_context=request.owner_context,
            activity_id=request.activity_id,
            attempt=request.attempt,
            worker_identity="wgcf-controlled-proof-unauthorized-probe",
            temporal_namespace=request.owner_context.temporal_namespace,
            task_queue=request.owner_context.activity_task_queue,
            workflow_id=request.workflow_id,
            workflow_run_id=request.workflow_run_id,
            source_revision=request.owner_context.wgcf_source_revision,
            started_at=request.started_at,
        )
    except ControlledProofIdentityDenied:
        return
    raise RuntimeError("controlled-proof identity probe did not fail closed")


def _prove_payload_boundary(request: AuthorizedControlledProofRequest) -> None:
    expanded = dict(request.normal_activity_payload)
    expanded["raw_output"] = "denied"
    try:
        ValidationReadinessActivityRequest.from_payload(expanded)
    except ValidationReadinessContractError:
        return
    raise RuntimeError("controlled-proof payload probe did not fail closed")


def _commit_expected_boundary_receipt(
    request: AuthorizedControlledProofRequest,
    observation_kind: str,
) -> None:
    _assert_controlled_context_remains_current(request)
    commit_controlled_proof_owner_receipt(
        request,
        evidence_root=_controlled_proof_evidence_root(),
        owner_result="passed",
        observation_kind=observation_kind,
        process_group_fenced=True,
    )


def _assert_controlled_context_remains_current(
    request: AuthorizedControlledProofRequest,
) -> None:
    context_path = os.environ.get(CONTROLLED_PROOF_CONTEXT_PATH_ENV, "").strip()
    context_digest = os.environ.get(
        CONTROLLED_PROOF_CONTEXT_DIGEST_ENV,
        "",
    ).strip()
    current = load_controlled_proof_owner_context(
        context_path,
        expected_digest=context_digest,
    )
    if current != request.owner_context:
        raise ControlledProofAuthorizationError(
            "controlled-proof owner context changed during activity execution",
        )
    if controlled_proof_image_source_revision() != current.wgcf_source_revision:
        raise ControlledProofAuthorizationError(
            "controlled-proof worker image provenance changed during execution",
        )
    if datetime.now(timezone.utc) >= current.authorization_expires_at:
        commit_controlled_proof_owner_receipt(
            request,
            evidence_root=_controlled_proof_evidence_root(),
            owner_result="failed",
            observation_kind="authorization-expired-before-result",
            process_group_fenced=True,
        )
        raise ControlledProofAuthorizationError(
            "controlled-proof authorization expired before result commit",
        )


def _commit_controlled_cancellation_receipt(
    request: AuthorizedControlledProofRequest,
) -> None:
    _assert_controlled_context_remains_current(request)
    now = datetime.now(timezone.utc)
    expected = (
        request.scenario.scenario_id == "cancellation"
        and now < request.owner_context.authorization_expires_at
    )
    commit_controlled_proof_owner_receipt(
        request,
        evidence_root=_controlled_proof_evidence_root(),
        owner_result="passed" if expected else "cancelled",
        observation_kind=(
            "activity-cancellation-fenced"
            if expected
            else "unexpected-activity-cancellation"
        ),
        process_group_fenced=True,
        recorded_at=now,
    )


def _controlled_proof_evidence_root() -> Path:
    return controlled_proof_evidence_root()


def _controlled_proof_activity_evidence_root(
    request: AuthorizedControlledProofRequest,
) -> Path:
    context_key = request.owner_context.owner_context_digest.removeprefix("sha256:")
    return _controlled_proof_evidence_root() / "activity-executions" / context_key


async def _run_fenced_owner_execution(
    envelope: dict[str, Any],
    *,
    evidence_root: Path | None = None,
) -> dict[str, Any]:
    payload = envelope["payload"]
    request = ValidationReadinessActivityRequest.from_payload(payload)
    if request.workflow_id != envelope["activity_context"]["workflow_id"]:
        raise ValidationReadinessContractError(
            "workflow_id must match the Temporal execution context",
        )
    evidence_root = (
        evidence_root
        or Path(
            os.environ.get(
                EVIDENCE_ROOT_ENV,
                "/var/lib/wgcf/orchestration/validation-readiness",
            ),
        )
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
    key_digest = sha256(request.idempotency_key.encode("utf-8")).hexdigest()
    committed_root = evidence_root / "committed" / key_digest
    owner_environment = dict(os.environ)
    owner_environment[EVIDENCE_ROOT_ENV] = str(staging_root)
    owner_environment[ARTIFACT_REFERENCE_ROOT_ENV] = str(
        committed_root / "runs" / key_digest[:24] / "artifacts",
    )
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
                group_fenced, cancellation_requested = (
                    await _stop_owner_process_before_acknowledgement(
                        process,
                        termination_grace_seconds,
                        fence_confirmation_seconds,
                    )
                )
                if cancellation_requested:
                    raise asyncio.CancelledError
                if not group_fenced:
                    raise OSError(
                        "WGCF owner process group termination was not confirmed",
                    )
                await _drain_communication(communication)
                raise TimeoutError("WGCF owner execution exceeded its bounded runtime")
            try:
                stdout, _stderr = await asyncio.wait_for(
                    asyncio.shield(communication),
                    timeout=min(heartbeat_interval_seconds, remaining),
                )
                stop_attempted = True
                group_fenced, cancellation_requested = (
                    await _stop_owner_process_before_acknowledgement(
                        process,
                        termination_grace_seconds,
                        fence_confirmation_seconds,
                    )
                )
                if cancellation_requested:
                    raise asyncio.CancelledError
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
            group_fenced, _cancellation_requested = (
                await _stop_owner_process_before_acknowledgement(
                    process,
                    termination_grace_seconds,
                    fence_confirmation_seconds,
                )
            )
        if communication is not None:
            await _drain_communication(communication)
        if process is not None and not group_fenced:
            raise OSError(
                "WGCF owner process group termination was not confirmed",
            )
        raise
    except BaseException:
        if process is not None and not stop_attempted:
            stop_attempted = True
            group_fenced, cancellation_requested = (
                await _stop_owner_process_before_acknowledgement(
                    process,
                    termination_grace_seconds,
                    fence_confirmation_seconds,
                )
            )
            if cancellation_requested:
                raise asyncio.CancelledError
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


async def _stop_owner_process_before_acknowledgement(
    process: asyncio.subprocess.Process,
    grace_seconds: float,
    fence_confirmation_seconds: float,
) -> tuple[bool, bool]:
    """Keep bounded process cleanup alive while recording cancellation."""

    cleanup = asyncio.create_task(
        _stop_owner_process(
            process,
            grace_seconds,
            fence_confirmation_seconds,
        ),
    )
    cancellation_requested = False
    while True:
        try:
            fenced = await asyncio.shield(cleanup)
            return fenced, cancellation_requested
        except asyncio.CancelledError:
            if cleanup.done():
                return cleanup.result(), True
            cancellation_requested = True


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
