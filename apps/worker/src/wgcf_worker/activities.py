"""Temporal adapters for WGCF-owned orchestration activities."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from control_fabric_core.orchestration_activities import (
    VALIDATION_READINESS_ACTIVITY_NAME,
    ValidationReadinessActivityContext,
    classify_validation_readiness_exception,
    execute_validation_readiness_activity,
)


WORKSPACE_ROOT_ENV = "WGCF_WORKSPACE_ROOT"
REPO_ROOT_ENV = "WGCF_REPO_ROOT"
EVIDENCE_ROOT_ENV = "WGCF_ORCHESTRATION_EVIDENCE_ROOT"
WORKER_ID_ENV = "WGCF_TEMPORAL_WORKER_ID"


@activity.defn(name=VALIDATION_READINESS_ACTIVITY_NAME)
async def validation_readiness_activity(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the bounded validation/readiness activity off the event loop."""

    info = activity.info()
    execution = asyncio.create_task(
        asyncio.to_thread(
            execute_validation_readiness_activity,
            payload,
            activity_context=ValidationReadinessActivityContext(
                activity_id=info.activity_id,
                attempt=info.attempt,
                worker_id=os.environ.get(WORKER_ID_ENV, "wgcf-activity-worker"),
                workflow_id=info.workflow_id,
            ),
            evidence_root=Path(
                os.environ.get(
                    EVIDENCE_ROOT_ENV,
                    "/var/lib/wgcf/orchestration/validation-readiness",
                ),
            ),
            repo_root=Path(
                os.environ.get(
                    REPO_ROOT_ENV,
                    "/workspace/workspace-governance-control-fabric",
                ),
            ),
            workspace_root=Path(os.environ.get(WORKSPACE_ROOT_ENV, "/workspace")),
        ),
    )
    try:
        return await asyncio.shield(execution)
    except asyncio.CancelledError:
        await _wait_for_execution(execution)
        raise
    except Exception as exc:
        failure = classify_validation_readiness_exception(exc)
        raise ApplicationError(
            failure.public_message,
            type=failure.error_type,
            non_retryable=not failure.retryable,
        ) from None


async def _wait_for_execution(execution: asyncio.Task[dict[str, Any]]) -> None:
    """Do not acknowledge cancellation while synchronous owner work is running."""

    while not execution.done():
        try:
            await asyncio.shield(execution)
        except asyncio.CancelledError:
            continue
        except Exception:
            break

    if execution.cancelled():
        return
    try:
        execution.result()
    except Exception:
        # Cancellation remains the authoritative activity outcome. Retrieving the
        # result here prevents an unobserved task failure after the thread exits.
        pass
