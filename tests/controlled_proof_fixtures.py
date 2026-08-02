from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from control_fabric_core.controlled_proof import (
    CONTROLLED_PROOF_RECEIPT_OWNERS,
    CONTROLLED_PROOF_SCENARIOS,
)


WGCF_REVISION = "d" * 40
OOS_REVISION = "c" * 40
CONTEXT_STARTED_AT = datetime(2026, 8, 2, 0, 2, tzinfo=timezone.utc)
ACTIVITY_STARTED_AT = datetime(2026, 8, 2, 0, 3, tzinfo=timezone.utc)


def valid_owner_context() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "owner_context_id": (
            "platform-controlled-proof://owner-contexts/"
            "commissioning-session-698-1/wgcf"
        ),
        "owner_repo": "workspace-governance-control-fabric",
        "orchestration_context": {
            "context_id": (
                "platform-controlled-proof://contexts/commissioning-session-698-1"
            ),
            "context_digest": f"sha256:{'a' * 64}",
        },
        "authorization": {
            "authorization_id": (
                "workspace-governance://controlled-runtime-proof/"
                "authorization-698-1"
            ),
            "authorization_digest": f"sha256:{'1' * 64}",
            "canonical_claims_digest": f"sha256:{'2' * 64}",
            "operator_approval_ref": (
                "openproject://work_packages/792/operator-approval"
            ),
            "operator_approval_digest": f"sha256:{'3' * 64}",
            "security_authorization_ref": (
                "security-architecture://authorizations/controlled-proof-698-1"
            ),
            "security_authorization_digest": f"sha256:{'4' * 64}",
            "consumption_receipt_ref": (
                "platform-engineering://controlled-proof/consumption-698-1"
            ),
            "consumption_receipt_digest": f"sha256:{'5' * 64}",
            "issued_at": "2026-08-02T00:00:00.000Z",
            "consumed_at": "2026-08-02T00:01:00.000Z",
            "expires_at": "2099-12-31T23:59:59.000Z",
        },
        "commissioning_session": {
            "commissioning_session_id": "commissioning-session-698-1",
            "started_at": "2026-08-02T00:02:00.000Z",
            "scenario_executions": [
                {
                    "scenario_id": scenario_id,
                    "scenario_execution_id": f"scenario-execution-{index:02d}",
                    "required_receipt_owners": (
                        ["platform-engineering"]
                        if scenario_id == "exact-baseline-restore"
                        else list(CONTROLLED_PROOF_RECEIPT_OWNERS)
                    ),
                }
                for index, scenario_id in enumerate(
                    CONTROLLED_PROOF_SCENARIOS,
                    start=1,
                )
            ],
        },
        "definition": {
            "definition_id": "validation-readiness-run",
            "definition_version": 1,
        },
        "request_binding": {
            "source_record_ref": "art:delivery-698",
            "source_version_ref": (
                "git:workspace-governance-control-fabric:" f"{WGCF_REVISION}"
            ),
            "operator_id": "operator:mfshaf7",
        },
        "runtime": {
            "profile_id": "temporal",
            "profile_lifecycle": "build-admitted",
            "environment": "dev-integration",
            "temporal_address": "temporal-frontend.temporal.svc:7233",
            "temporal_namespace": "default",
            "worker_identity": "wgcf-controlled-proof-activity-worker",
            "activity_task_queue": (
                "wgcf.controlled-proof.validation-readiness.v1"
            ),
        },
        "source_revisions": {
            "operator_orchestration_service": OOS_REVISION,
            "workspace_governance_control_fabric": WGCF_REVISION,
        },
    }


def write_owner_context(root: Path) -> tuple[Path, str]:
    path = root / "wgcf-controlled-proof-owner-context.json"
    raw = json.dumps(valid_owner_context(), indent=2, sort_keys=True) + "\n"
    path.write_text(raw, encoding="utf-8")
    path.chmod(0o600)
    return path, f"sha256:{sha256(raw.encode('utf-8')).hexdigest()}"


def valid_controlled_request(
    *,
    scenario_index: int = 0,
    idempotency_key: str = "activity:controlled-proof:scenario-01:attempt-1",
) -> dict[str, Any]:
    context = valid_owner_context()
    scenario = context["commissioning_session"]["scenario_executions"][
        scenario_index
    ]
    return {
        "schema_version": 1,
        "definition_id": "validation-readiness-run",
        "definition_version": 1,
        "run_id": "temporal-execution:controlled-proof-698-01",
        "workflow_id": "workflow:controlled-proof-698-01",
        "source_ref": context["request_binding"]["source_record_ref"],
        "source_version": context["request_binding"]["source_version_ref"],
        "validation_scope": "component:workspace-governance",
        "readiness_target": "repo:workspace-governance-control-fabric",
        "profile": "local-read-only",
        "tier": "smoke",
        "correlation_id": "controlled-proof-session:698-1",
        "causation_id": "controlled-proof-authorization:698-1",
        "idempotency_key": idempotency_key,
        "caller_id": "operator-orchestration-service-controlled-proof",
        "operator_id": context["request_binding"]["operator_id"],
        "controlled_proof_execution": {
            "authorization_consumed_at": context["authorization"]["consumed_at"],
            "authorization_digest": context["authorization"][
                "authorization_digest"
            ],
            "authorization_expires_at": context["authorization"]["expires_at"],
            "authorization_id": context["authorization"]["authorization_id"],
            "canonical_claims_digest": context["authorization"][
                "canonical_claims_digest"
            ],
            "commissioning_session_id": context["commissioning_session"][
                "commissioning_session_id"
            ],
            "commissioning_session_started_at": context[
                "commissioning_session"
            ]["started_at"],
            "context_digest": context["orchestration_context"]["context_digest"],
            "context_id": context["orchestration_context"]["context_id"],
            "environment": "dev-integration",
            "oos_source_revision": OOS_REVISION,
            "profile_lifecycle": "build-admitted",
            "required_receipt_owners": list(
                scenario["required_receipt_owners"],
            ),
            "scenario_execution_id": scenario["scenario_execution_id"],
            "scenario_id": scenario["scenario_id"],
            "wgcf_source_revision": WGCF_REVISION,
        },
    }


def controlled_worker_env(context_path: Path, context_digest: str) -> dict[str, str]:
    return {
        "WGCF_CONTROLLED_PROOF_ENABLED": "true",
        "WGCF_CONTROLLED_PROOF_EXECUTION_AUTHORIZED": "true",
        "WGCF_CONTROLLED_PROOF_CONTEXT_PATH": str(context_path),
        "WGCF_CONTROLLED_PROOF_CONTEXT_DIGEST": context_digest,
        "WGCF_CONTROLLED_PROOF_SOURCE_REVISION": WGCF_REVISION,
        "WGCF_CONTROLLED_PROOF_EVIDENCE_ROOT": str(
            context_path.parent / "evidence",
        ),
        "WGCF_CONTROLLED_PROOF_TEMPORAL_ADDRESS": (
            "temporal-frontend.temporal.svc:7233"
        ),
        "WGCF_CONTROLLED_PROOF_TEMPORAL_NAMESPACE": "default",
        "WGCF_CONTROLLED_PROOF_TEMPORAL_TASK_QUEUE": (
            "wgcf.controlled-proof.validation-readiness.v1"
        ),
        "WGCF_CONTROLLED_PROOF_TEMPORAL_WORKER_ID": (
            "wgcf-controlled-proof-activity-worker"
        ),
    }
