"""Isolated process entrypoint for bounded WGCF orchestration owner work."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from control_fabric_core.orchestration_activities import (
    ValidationReadinessActivityContext,
    classify_validation_readiness_exception,
    execute_validation_readiness_activity,
)


WORKSPACE_ROOT_ENV = "WGCF_WORKSPACE_ROOT"
REPO_ROOT_ENV = "WGCF_REPO_ROOT"
EVIDENCE_ROOT_ENV = "WGCF_ORCHESTRATION_EVIDENCE_ROOT"
_PROTOCOL_SCHEMA_VERSION = 1


def execute_owner_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Execute one parent-generated request and return only a bounded envelope."""

    try:
        if envelope.get("schema_version") != _PROTOCOL_SCHEMA_VERSION:
            raise ValueError("unsupported owner execution protocol")
        payload = envelope["payload"]
        context = envelope["activity_context"]
        if not isinstance(payload, Mapping) or not isinstance(context, Mapping):
            raise ValueError("invalid owner execution envelope")
        result = execute_validation_readiness_activity(
            payload,
            activity_context=ValidationReadinessActivityContext(
                activity_id=str(context["activity_id"]),
                attempt=context["attempt"],
                worker_id=str(context["worker_id"]),
                workflow_id=str(context["workflow_id"]),
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
        return {
            "schema_version": _PROTOCOL_SCHEMA_VERSION,
            "status": "completed",
            "result": result,
        }
    except BaseException as exc:
        failure = classify_validation_readiness_exception(exc)
        return {
            "schema_version": _PROTOCOL_SCHEMA_VERSION,
            "status": "failed",
            "failure": {
                "error_type": failure.error_type,
                "public_message": failure.public_message,
                "retryable": failure.retryable,
            },
        }


def main() -> int:
    try:
        envelope = json.load(sys.stdin)
        if not isinstance(envelope, Mapping):
            raise ValueError("owner execution input must be an object")
        response = execute_owner_envelope(envelope)
    except BaseException as exc:
        failure = classify_validation_readiness_exception(exc)
        response = {
            "schema_version": _PROTOCOL_SCHEMA_VERSION,
            "status": "failed",
            "failure": {
                "error_type": failure.error_type,
                "public_message": failure.public_message,
                "retryable": failure.retryable,
            },
        }
    json.dump(response, sys.stdout, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
