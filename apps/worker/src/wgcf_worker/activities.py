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
    ValidationReadinessContractError,
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
    try:
        return await asyncio.to_thread(
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
        )
    except ValidationReadinessContractError as exc:
        raise ApplicationError(
            str(exc),
            type=type(exc).__name__,
            non_retryable=True,
        ) from exc
