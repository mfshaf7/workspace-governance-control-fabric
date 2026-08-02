"""Permit-bound WGCF controlled-proof context and owner receipts."""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


CONTROLLED_PROOF_OWNER_REPO = "workspace-governance-control-fabric"
CONTROLLED_PROOF_ACTIVITY_CALLER_ID = (
    "operator-orchestration-service-controlled-proof"
)
CONTROLLED_PROOF_ACTIVITY_TASK_QUEUE = (
    "wgcf.controlled-proof.validation-readiness.v1"
)
CONTROLLED_PROOF_WORKER_ID = "wgcf-controlled-proof-activity-worker"
CONTROLLED_PROOF_CONTEXT_SCHEMA_VERSION = 1
CONTROLLED_PROOF_REQUEST_SCHEMA_VERSION = 1
CONTROLLED_PROOF_RECEIPT_SCHEMA_VERSION = 1
CONTROLLED_PROOF_SCENARIOS = (
    "nominal-completion",
    "workflow-worker-restart",
    "temporal-runtime-restart",
    "deterministic-replay",
    "duplicate-suppression",
    "cancellation",
    "unavailable-dependency",
    "identity-denial",
    "payload-boundary",
    "backup-restore",
    "exact-baseline-restore",
)
CONTROLLED_PROOF_RECEIPT_OWNERS = (
    "platform-engineering",
    "operator-orchestration-service",
    CONTROLLED_PROOF_OWNER_REPO,
)
CONTROLLED_PROOF_NEGATIVE_SCENARIOS = (
    "unavailable-dependency",
    "identity-denial",
    "payload-boundary",
)

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_REVISION_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_URI_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]*://[^\s]{1,500}$")
_MAX_CONTEXT_BYTES = 256 * 1024

_OWNER_CONTEXT_FIELDS = frozenset(
    {
        "schema_version",
        "owner_context_id",
        "owner_repo",
        "orchestration_context",
        "authorization",
        "commissioning_session",
        "definition",
        "request_binding",
        "runtime",
        "source_revisions",
    },
)
_CONTROLLED_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "definition_id",
        "definition_version",
        "run_id",
        "workflow_id",
        "source_ref",
        "source_version",
        "validation_scope",
        "readiness_target",
        "profile",
        "tier",
        "correlation_id",
        "causation_id",
        "idempotency_key",
        "caller_id",
        "operator_id",
        "controlled_proof_execution",
    },
)
_EXECUTION_FIELDS = frozenset(
    {
        "authorization_consumed_at",
        "authorization_digest",
        "authorization_expires_at",
        "authorization_id",
        "canonical_claims_digest",
        "commissioning_session_id",
        "commissioning_session_started_at",
        "context_digest",
        "context_id",
        "environment",
        "oos_source_revision",
        "profile_lifecycle",
        "required_receipt_owners",
        "scenario_execution_id",
        "scenario_id",
        "wgcf_source_revision",
    },
)
_OWNER_RECEIPT_FIELDS = frozenset(
    {
        "owner_repo",
        "authorization_id",
        "authorization_digest",
        "commissioning_session_id",
        "scenario_id",
        "scenario_execution_id",
        "owner_execution",
        "owner_result",
        "evidence_refs",
        "receipt_ref",
        "receipt_digest",
        "recorded_at",
    },
)


class ControlledProofContractError(ValueError):
    """Raised when controlled-proof input is outside the WGCF boundary."""


class ControlledProofAuthorizationError(ControlledProofContractError):
    """Raised when the permit-derived execution context is not authorized."""


class ControlledProofContextMismatch(ControlledProofContractError):
    """Raised when a request or replay does not match the mounted context."""


class ControlledProofIdentityDenied(ControlledProofContractError):
    """Raised when a runtime identity is outside the mounted context."""


class ControlledProofPayloadRejected(ControlledProofContractError):
    """Raised when the bounded payload assertion rejects expanded content."""


@dataclass(frozen=True)
class ControlledProofScenarioExecution:
    """One authorization-enumerated commissioning scenario."""

    scenario_id: str
    scenario_execution_id: str
    required_receipt_owners: tuple[str, ...]


@dataclass(frozen=True)
class ControlledProofOwnerContext:
    """WGCF's independently pinned projection of one consumed permit."""

    owner_context_id: str
    owner_context_digest: str
    orchestration_context_id: str
    orchestration_context_digest: str
    authorization_id: str
    authorization_digest: str
    canonical_claims_digest: str
    operator_approval_ref: str
    operator_approval_digest: str
    security_authorization_ref: str
    security_authorization_digest: str
    consumption_receipt_ref: str
    consumption_receipt_digest: str
    authorization_issued_at: datetime
    authorization_consumed_at: datetime
    authorization_expires_at: datetime
    commissioning_session_id: str
    commissioning_session_started_at: datetime
    scenario_executions: tuple[ControlledProofScenarioExecution, ...]
    source_record_ref: str
    source_version_ref: str
    operator_id: str
    temporal_address: str
    temporal_namespace: str
    worker_identity: str
    activity_task_queue: str
    oos_source_revision: str
    wgcf_source_revision: str

    def scenario(self, execution_id: str) -> ControlledProofScenarioExecution:
        for scenario in self.scenario_executions:
            if scenario.scenario_execution_id == execution_id:
                return scenario
        raise ControlledProofAuthorizationError(
            "scenario execution is not present in the mounted WGCF context",
        )


@dataclass(frozen=True)
class AuthorizedControlledProofRequest:
    """A request proven against both Temporal metadata and owner context."""

    payload: dict[str, Any]
    normal_activity_payload: dict[str, Any]
    request_digest: str
    owner_context: ControlledProofOwnerContext
    scenario: ControlledProofScenarioExecution
    activity_id: str
    attempt: int
    workflow_id: str
    workflow_run_id: str
    started_at: datetime


def load_controlled_proof_owner_context(
    context_path: str | Path,
    *,
    expected_digest: str,
) -> ControlledProofOwnerContext:
    """Load one strict owner context and verify its exact raw-byte digest."""

    _require_digest(expected_digest, "owner context digest")
    path = Path(context_path).resolve()
    try:
        stat = path.stat()
    except OSError as exc:
        raise ControlledProofAuthorizationError(
            "controlled-proof owner context is unavailable",
        ) from exc
    if not path.is_file():
        raise ControlledProofAuthorizationError(
            "controlled-proof owner context must be a regular file",
        )
    if stat.st_mode & 0o022:
        raise ControlledProofAuthorizationError(
            "controlled-proof owner context must not be group or world writable",
        )
    if stat.st_size > _MAX_CONTEXT_BYTES:
        raise ControlledProofAuthorizationError(
            "controlled-proof owner context exceeds the bounded size",
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ControlledProofAuthorizationError(
            "controlled-proof owner context is unavailable",
        ) from exc
    actual_digest = f"sha256:{sha256(raw).hexdigest()}"
    if actual_digest != expected_digest:
        raise ControlledProofContextMismatch(
            "controlled-proof owner context digest does not match",
        )
    try:
        record = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlledProofContractError(
            "controlled-proof owner context is not valid JSON",
        ) from exc
    return _parse_owner_context(record, owner_context_digest=actual_digest)


def authorize_controlled_proof_activity_request(
    payload: Mapping[str, Any],
    *,
    owner_context: ControlledProofOwnerContext,
    activity_id: str,
    attempt: int,
    worker_identity: str,
    temporal_namespace: str,
    task_queue: str,
    workflow_id: str,
    workflow_run_id: str,
    source_revision: str,
    started_at: datetime,
) -> AuthorizedControlledProofRequest:
    """Fail closed unless every owner, request, and runtime binding agrees."""

    _require_exact_fields(payload, _CONTROLLED_REQUEST_FIELDS, "activity request")
    if payload.get("caller_id") != CONTROLLED_PROOF_ACTIVITY_CALLER_ID:
        raise ControlledProofIdentityDenied(
            "controlled-proof activity caller is not authorized",
        )
    if payload.get("schema_version") != CONTROLLED_PROOF_REQUEST_SCHEMA_VERSION:
        raise ControlledProofContractError(
            "controlled-proof request schema_version is unsupported",
        )
    execution = payload.get("controlled_proof_execution")
    _require_mapping(execution, "controlled_proof_execution")
    assert isinstance(execution, Mapping)
    _require_exact_fields(execution, _EXECUTION_FIELDS, "controlled_proof_execution")

    if worker_identity != owner_context.worker_identity:
        raise ControlledProofIdentityDenied(
            "controlled-proof worker identity does not match the mounted context",
        )
    _require_equal(
        temporal_namespace,
        owner_context.temporal_namespace,
        "Temporal namespace",
    )
    _require_equal(task_queue, owner_context.activity_task_queue, "activity task queue")
    _require_equal(source_revision, owner_context.wgcf_source_revision, "WGCF source revision")
    _require_equal(str(payload.get("workflow_id")), workflow_id, "workflow id")
    _require_equal(str(payload.get("run_id")), workflow_run_id, "workflow run id")
    _require_identifier(activity_id, "activity_id")
    _require_identifier(workflow_id, "workflow_id")
    _require_identifier(workflow_run_id, "workflow_run_id")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ControlledProofContractError("attempt must be an integer of at least 1")

    started = _as_utc(started_at, "activity started_at")
    if started < owner_context.commissioning_session_started_at:
        raise ControlledProofAuthorizationError(
            "activity started before the authorized commissioning session",
        )
    if started >= owner_context.authorization_expires_at:
        raise ControlledProofAuthorizationError(
            "activity started after controlled-proof authorization expiry",
        )

    scenario_execution_id = _require_identifier(
        execution.get("scenario_execution_id"),
        "controlled_proof_execution.scenario_execution_id",
    )
    scenario = owner_context.scenario(scenario_execution_id)
    requested_owners = _require_receipt_owners(
        execution.get("required_receipt_owners"),
        "controlled_proof_execution.required_receipt_owners",
    )
    if CONTROLLED_PROOF_OWNER_REPO not in requested_owners:
        raise ControlledProofAuthorizationError(
            "scenario execution does not authorize a WGCF owner receipt",
        )

    expected = {
        "context_id": owner_context.orchestration_context_id,
        "context_digest": owner_context.orchestration_context_digest,
        "authorization_id": owner_context.authorization_id,
        "authorization_digest": owner_context.authorization_digest,
        "canonical_claims_digest": owner_context.canonical_claims_digest,
        "commissioning_session_id": owner_context.commissioning_session_id,
        "scenario_id": scenario.scenario_id,
        "required_receipt_owners": list(scenario.required_receipt_owners),
        "profile_lifecycle": "build-admitted",
        "environment": "dev-integration",
        "oos_source_revision": owner_context.oos_source_revision,
        "wgcf_source_revision": owner_context.wgcf_source_revision,
    }
    mismatched = [
        field
        for field, required in expected.items()
        if execution.get(field) != required
    ]
    if mismatched:
        raise ControlledProofContextMismatch(
            "controlled-proof execution does not match the mounted owner context: "
            + ", ".join(sorted(mismatched)),
        )
    timestamp_bindings = {
        "authorization_consumed_at": owner_context.authorization_consumed_at,
        "authorization_expires_at": owner_context.authorization_expires_at,
        "commissioning_session_started_at": (
            owner_context.commissioning_session_started_at
        ),
    }
    mismatched_timestamps = [
        field
        for field, required in timestamp_bindings.items()
        if _require_timestamp(
            execution.get(field),
            f"controlled_proof_execution.{field}",
        )
        != required
    ]
    if mismatched_timestamps:
        raise ControlledProofContextMismatch(
            "controlled-proof execution does not match the mounted owner context: "
            + ", ".join(sorted(mismatched_timestamps)),
        )
    _require_equal(
        str(payload.get("source_ref")),
        owner_context.source_record_ref,
        "source_ref",
    )
    _require_equal(
        str(payload.get("source_version")),
        owner_context.source_version_ref,
        "source_version",
    )
    _require_equal(
        str(payload.get("operator_id")),
        owner_context.operator_id,
        "operator_id",
    )

    normalized_payload = _plain_record(payload)
    normal_payload = {
        key: value
        for key, value in normalized_payload.items()
        if key != "controlled_proof_execution"
    }
    normal_payload["caller_id"] = "operator-orchestration-service"
    return AuthorizedControlledProofRequest(
        payload=normalized_payload,
        normal_activity_payload=normal_payload,
        request_digest=_record_digest(
            {
                "owner_context_digest": owner_context.owner_context_digest,
                "payload": normalized_payload,
            },
        ),
        owner_context=owner_context,
        scenario=scenario,
        activity_id=activity_id,
        attempt=attempt,
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        started_at=started,
    )


def bind_controlled_proof_request(
    request: AuthorizedControlledProofRequest,
    *,
    evidence_root: str | Path,
) -> Path:
    """Bind one idempotency key to one owner context and full request."""

    root = Path(evidence_root).resolve()
    idempotency_key = str(request.payload["idempotency_key"])
    key_digest = sha256(idempotency_key.encode("utf-8")).hexdigest()
    binding_path = root / "bindings" / f"{key_digest}.json"
    scenario_key = _scenario_binding_key(request)
    scenario_path = root / "scenario-bindings" / f"{scenario_key}.json"
    scenario_lock_path = root / "locks" / f"scenario-{scenario_key}.lock"
    scenario_record = {
        "schema_version": CONTROLLED_PROOF_REQUEST_SCHEMA_VERSION,
        "owner_context_digest": request.owner_context.owner_context_digest,
        "authorization_digest": request.owner_context.authorization_digest,
        "commissioning_session_id": request.owner_context.commissioning_session_id,
        "scenario_execution_id": request.scenario.scenario_execution_id,
        "workflow_id": request.workflow_id,
        "workflow_run_id": request.workflow_run_id,
    }
    with _exclusive_lock(scenario_lock_path):
        existing_scenario = _load_json(scenario_path)
        if existing_scenario is not None and existing_scenario != scenario_record:
            raise ControlledProofContextMismatch(
                "controlled-proof scenario execution is bound to another workflow run",
            )
        if existing_scenario is None:
            _write_json_atomic(scenario_path, scenario_record)

    lock_path = root / "locks" / f"binding-{key_digest}.lock"
    record = {
        "schema_version": CONTROLLED_PROOF_REQUEST_SCHEMA_VERSION,
        "idempotency_key_digest": f"sha256:{key_digest}",
        "owner_context_digest": request.owner_context.owner_context_digest,
        "request_digest": request.request_digest,
    }
    with _exclusive_lock(lock_path):
        existing = _load_json(binding_path)
        if existing is not None and existing != record:
            raise ControlledProofContextMismatch(
                "controlled-proof idempotency key is bound to another context or request",
            )
        if existing is None:
            _write_json_atomic(binding_path, record)
    return binding_path


def commit_controlled_proof_owner_receipt(
    request: AuthorizedControlledProofRequest,
    *,
    evidence_root: str | Path,
    owner_result: str,
    observation_kind: str,
    process_group_fenced: bool,
    activity_result: Mapping[str, Any] | None = None,
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """Persist one deterministic WGCF receipt suitable for the proof result."""

    if owner_result not in {
        "passed",
        "failed",
        "blocked",
        "cancelled",
        "denied",
        "unavailable",
    }:
        raise ControlledProofContractError("unsupported controlled-proof owner result")
    if not process_group_fenced:
        raise ControlledProofAuthorizationError(
            "WGCF owner process group was not fenced before receipt commit",
        )
    normalized_observation_kind = _require_identifier(
        observation_kind,
        "observation_kind",
    )
    normalized_activity_result = _normalize_activity_result(activity_result)
    root = Path(evidence_root).resolve()
    receipt_key = _owner_receipt_key(request)
    receipt_path = root / "receipts" / f"{receipt_key}.json"
    observation_path = root / "observations" / f"{receipt_key}.json"
    activity_result_path = root / "activity-results" / f"{receipt_key}.json"
    lock_path = root / "locks" / f"receipt-{receipt_key}.lock"

    with _exclusive_lock(lock_path):
        existing = _load_json(receipt_path)
        if existing is not None:
            _assert_existing_receipt(
                existing,
                request,
                owner_result,
                evidence_root=root,
                observation_kind=normalized_observation_kind,
                activity_result=normalized_activity_result,
            )
            return dict(existing)

        receipt_time = _as_utc(
            recorded_at or datetime.now(timezone.utc),
            "recorded_at",
        )
        if receipt_time < request.started_at:
            raise ControlledProofAuthorizationError(
                "controlled-proof receipt cannot predate activity execution",
            )
        if (
            owner_result == "passed"
            and receipt_time >= request.owner_context.authorization_expires_at
        ):
            raise ControlledProofAuthorizationError(
                "a passing controlled-proof receipt must be recorded before expiry",
            )
        timestamp = _format_timestamp(receipt_time)
        observation = {
            "schema_version": CONTROLLED_PROOF_RECEIPT_SCHEMA_VERSION,
            "owner_repo": CONTROLLED_PROOF_OWNER_REPO,
            "authorization_id": request.owner_context.authorization_id,
            "authorization_digest": request.owner_context.authorization_digest,
            "commissioning_session_id": request.owner_context.commissioning_session_id,
            "scenario_id": request.scenario.scenario_id,
            "scenario_execution_id": request.scenario.scenario_execution_id,
            "activity_id": request.activity_id,
            "workflow_id": request.workflow_id,
            "workflow_run_id": request.workflow_run_id,
            "observation_kind": normalized_observation_kind,
            "owner_result": owner_result,
            "process_group_fenced": True,
            "recorded_at": timestamp,
        }
        if normalized_activity_result is not None:
            observation["activity_result_digest"] = _record_digest(
                normalized_activity_result,
            )
            observation["activity_status_code"] = _require_identifier(
                normalized_activity_result.get("status_code"),
                "activity_result.status_code",
            )
            _write_or_verify_json(
                activity_result_path,
                normalized_activity_result,
                "controlled-proof activity result",
            )
        _write_or_verify_json(
            observation_path,
            observation,
            "controlled-proof observation",
        )
        observation_digest = _record_digest(observation)
        evidence_refs = _owner_receipt_evidence_refs(
            request,
            receipt_key=receipt_key,
            observation_digest=observation_digest,
            activity_result=normalized_activity_result,
        )

        receipt_ref = f"wgcf-controlled-proof://receipts/{receipt_key}"
        receipt_without_digest = {
            "owner_repo": CONTROLLED_PROOF_OWNER_REPO,
            "authorization_id": request.owner_context.authorization_id,
            "authorization_digest": request.owner_context.authorization_digest,
            "commissioning_session_id": request.owner_context.commissioning_session_id,
            "scenario_id": request.scenario.scenario_id,
            "scenario_execution_id": request.scenario.scenario_execution_id,
            "owner_execution": {
                "execution_type": "activity",
                "execution_id": request.activity_id,
            },
            "owner_result": owner_result,
            "evidence_refs": evidence_refs,
            "receipt_ref": receipt_ref,
            "recorded_at": timestamp,
        }
        receipt = {
            **receipt_without_digest,
            "receipt_digest": _record_digest(receipt_without_digest),
        }
        _write_json_atomic(receipt_path, receipt)
        return receipt


def load_controlled_proof_owner_receipt(
    request: AuthorizedControlledProofRequest,
    *,
    evidence_root: str | Path,
) -> dict[str, Any] | None:
    """Load the terminal receipt for one authorized activity execution."""

    path = (
        Path(evidence_root).resolve()
        / "receipts"
        / f"{_owner_receipt_key(request)}.json"
    )
    record = _load_json(path)
    if record is None:
        return None
    _validate_stored_receipt(
        record,
        request,
        evidence_root=Path(evidence_root).resolve(),
    )
    return dict(record)


def owner_result_for_activity_result(
    result: Mapping[str, Any],
    *,
    scenario_id: str,
) -> str:
    """Project the compact activity result into the owner-receipt vocabulary."""

    status_code = result.get("status_code")
    if status_code == "ready":
        if scenario_id in {*CONTROLLED_PROOF_NEGATIVE_SCENARIOS, "cancellation"}:
            return "blocked"
        return "passed"
    if status_code == "blocked":
        return "blocked"
    if status_code == "unavailable":
        return "unavailable"
    return "failed"


def _parse_owner_context(
    record: Any,
    *,
    owner_context_digest: str,
) -> ControlledProofOwnerContext:
    _require_mapping(record, "controlled-proof owner context")
    assert isinstance(record, Mapping)
    _require_exact_fields(record, _OWNER_CONTEXT_FIELDS, "owner context")
    if record.get("schema_version") != CONTROLLED_PROOF_CONTEXT_SCHEMA_VERSION:
        raise ControlledProofContractError("owner context schema_version is unsupported")
    _require_equal(record.get("owner_repo"), CONTROLLED_PROOF_OWNER_REPO, "owner_repo")

    orchestration = _required_object(
        record,
        "orchestration_context",
        {"context_id", "context_digest"},
    )
    authorization = _required_object(
        record,
        "authorization",
        {
            "authorization_id",
            "authorization_digest",
            "canonical_claims_digest",
            "operator_approval_ref",
            "operator_approval_digest",
            "security_authorization_ref",
            "security_authorization_digest",
            "consumption_receipt_ref",
            "consumption_receipt_digest",
            "issued_at",
            "consumed_at",
            "expires_at",
        },
    )
    session = _required_object(
        record,
        "commissioning_session",
        {"commissioning_session_id", "started_at", "scenario_executions"},
    )
    definition = _required_object(
        record,
        "definition",
        {"definition_id", "definition_version"},
    )
    request_binding = _required_object(
        record,
        "request_binding",
        {"source_record_ref", "source_version_ref", "operator_id"},
    )
    runtime = _required_object(
        record,
        "runtime",
        {
            "profile_id",
            "profile_lifecycle",
            "environment",
            "temporal_address",
            "temporal_namespace",
            "worker_identity",
            "activity_task_queue",
        },
    )
    revisions = _required_object(
        record,
        "source_revisions",
        {
            "operator_orchestration_service",
            "workspace_governance_control_fabric",
        },
    )

    _require_equal(definition.get("definition_id"), "validation-readiness-run", "definition_id")
    _require_equal(definition.get("definition_version"), 1, "definition_version")
    _require_equal(runtime.get("profile_id"), "temporal", "runtime.profile_id")
    _require_equal(
        runtime.get("profile_lifecycle"),
        "build-admitted",
        "runtime.profile_lifecycle",
    )
    _require_equal(runtime.get("environment"), "dev-integration", "runtime.environment")
    _require_equal(
        runtime.get("activity_task_queue"),
        CONTROLLED_PROOF_ACTIVITY_TASK_QUEUE,
        "runtime.activity_task_queue",
    )
    _require_equal(
        runtime.get("worker_identity"),
        CONTROLLED_PROOF_WORKER_ID,
        "runtime.worker_identity",
    )

    issued_at = _require_timestamp(authorization.get("issued_at"), "authorization.issued_at")
    consumed_at = _require_timestamp(
        authorization.get("consumed_at"),
        "authorization.consumed_at",
    )
    started_at = _require_timestamp(session.get("started_at"), "commissioning_session.started_at")
    expires_at = _require_timestamp(
        authorization.get("expires_at"),
        "authorization.expires_at",
    )
    if not issued_at < consumed_at < started_at < expires_at:
        raise ControlledProofAuthorizationError(
            "controlled-proof authorization and session timeline is inconsistent",
        )

    scenarios = _parse_scenario_executions(session.get("scenario_executions"))
    oos_revision = _require_revision(
        revisions.get("operator_orchestration_service"),
        "source_revisions.operator_orchestration_service",
    )
    wgcf_revision = _require_revision(
        revisions.get("workspace_governance_control_fabric"),
        "source_revisions.workspace_governance_control_fabric",
    )
    source_version_ref = _require_identifier(
        request_binding.get("source_version_ref"),
        "request_binding.source_version_ref",
    )
    _require_equal(
        source_version_ref,
        f"git:{CONTROLLED_PROOF_OWNER_REPO}:{wgcf_revision}",
        "request_binding.source_version_ref",
    )

    return ControlledProofOwnerContext(
        owner_context_id=_require_uri(record.get("owner_context_id"), "owner_context_id"),
        owner_context_digest=owner_context_digest,
        orchestration_context_id=_require_uri(
            orchestration.get("context_id"),
            "orchestration_context.context_id",
        ),
        orchestration_context_digest=_require_digest(
            orchestration.get("context_digest"),
            "orchestration_context.context_digest",
        ),
        authorization_id=_require_uri(
            authorization.get("authorization_id"),
            "authorization.authorization_id",
        ),
        authorization_digest=_require_digest(
            authorization.get("authorization_digest"),
            "authorization.authorization_digest",
        ),
        canonical_claims_digest=_require_digest(
            authorization.get("canonical_claims_digest"),
            "authorization.canonical_claims_digest",
        ),
        operator_approval_ref=_require_uri(
            authorization.get("operator_approval_ref"),
            "authorization.operator_approval_ref",
        ),
        operator_approval_digest=_require_digest(
            authorization.get("operator_approval_digest"),
            "authorization.operator_approval_digest",
        ),
        security_authorization_ref=_require_uri(
            authorization.get("security_authorization_ref"),
            "authorization.security_authorization_ref",
        ),
        security_authorization_digest=_require_digest(
            authorization.get("security_authorization_digest"),
            "authorization.security_authorization_digest",
        ),
        consumption_receipt_ref=_require_uri(
            authorization.get("consumption_receipt_ref"),
            "authorization.consumption_receipt_ref",
        ),
        consumption_receipt_digest=_require_digest(
            authorization.get("consumption_receipt_digest"),
            "authorization.consumption_receipt_digest",
        ),
        authorization_issued_at=issued_at,
        authorization_consumed_at=consumed_at,
        authorization_expires_at=expires_at,
        commissioning_session_id=_require_identifier(
            session.get("commissioning_session_id"),
            "commissioning_session.commissioning_session_id",
        ),
        commissioning_session_started_at=started_at,
        scenario_executions=scenarios,
        source_record_ref=_require_identifier(
            request_binding.get("source_record_ref"),
            "request_binding.source_record_ref",
        ),
        source_version_ref=source_version_ref,
        operator_id=_require_identifier(
            request_binding.get("operator_id"),
            "request_binding.operator_id",
        ),
        temporal_address=_require_identifier(
            runtime.get("temporal_address"),
            "runtime.temporal_address",
        ),
        temporal_namespace=_require_identifier(
            runtime.get("temporal_namespace"),
            "runtime.temporal_namespace",
        ),
        worker_identity=_require_identifier(
            runtime.get("worker_identity"),
            "runtime.worker_identity",
        ),
        activity_task_queue=_require_identifier(
            runtime.get("activity_task_queue"),
            "runtime.activity_task_queue",
        ),
        oos_source_revision=oos_revision,
        wgcf_source_revision=wgcf_revision,
    )


def _parse_scenario_executions(value: Any) -> tuple[ControlledProofScenarioExecution, ...]:
    if not isinstance(value, list) or len(value) != len(CONTROLLED_PROOF_SCENARIOS):
        raise ControlledProofContractError(
            "commissioning session must contain the exact controlled-proof scenario set",
        )
    scenarios: list[ControlledProofScenarioExecution] = []
    execution_ids: set[str] = set()
    for index, item in enumerate(value):
        _require_mapping(item, f"scenario_executions[{index}]")
        assert isinstance(item, Mapping)
        _require_exact_fields(
            item,
            {"scenario_id", "scenario_execution_id", "required_receipt_owners"},
            f"scenario_executions[{index}]",
        )
        scenario_id = _require_identifier(
            item.get("scenario_id"),
            f"scenario_executions[{index}].scenario_id",
        )
        _require_equal(
            scenario_id,
            CONTROLLED_PROOF_SCENARIOS[index],
            f"scenario_executions[{index}].scenario_id",
        )
        execution_id = _require_identifier(
            item.get("scenario_execution_id"),
            f"scenario_executions[{index}].scenario_execution_id",
        )
        if execution_id in execution_ids:
            raise ControlledProofContractError("scenario execution ids must be unique")
        execution_ids.add(execution_id)
        owners = _require_receipt_owners(
            item.get("required_receipt_owners"),
            f"scenario_executions[{index}].required_receipt_owners",
        )
        if (
            scenario_id == "exact-baseline-restore"
            and CONTROLLED_PROOF_OWNER_REPO in owners
        ):
            raise ControlledProofAuthorizationError(
                "exact-baseline restore cannot require a WGCF receipt after removal",
            )
        scenarios.append(
            ControlledProofScenarioExecution(
                scenario_id=scenario_id,
                scenario_execution_id=execution_id,
                required_receipt_owners=owners,
            ),
        )
    return tuple(scenarios)


def _assert_existing_receipt(
    receipt: Mapping[str, Any],
    request: AuthorizedControlledProofRequest,
    owner_result: str,
    *,
    evidence_root: Path,
    observation_kind: str,
    activity_result: dict[str, Any] | None,
) -> None:
    observation, stored_activity_result = _validate_stored_receipt(
        receipt,
        request,
        evidence_root=evidence_root,
    )
    mismatched = []
    if receipt.get("owner_result") != owner_result:
        mismatched.append("owner_result")
    if observation.get("observation_kind") != observation_kind:
        mismatched.append("observation_kind")
    if stored_activity_result != activity_result:
        mismatched.append("activity_result")
    if mismatched:
        raise ControlledProofContextMismatch(
            "existing controlled-proof receipt does not match: "
            + ", ".join(sorted(mismatched)),
        )


def _validate_stored_receipt(
    receipt: Mapping[str, Any],
    request: AuthorizedControlledProofRequest,
    *,
    evidence_root: Path,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    _require_exact_fields(receipt, _OWNER_RECEIPT_FIELDS, "owner receipt")
    expected = {
        "owner_repo": CONTROLLED_PROOF_OWNER_REPO,
        "authorization_id": request.owner_context.authorization_id,
        "authorization_digest": request.owner_context.authorization_digest,
        "commissioning_session_id": request.owner_context.commissioning_session_id,
        "scenario_id": request.scenario.scenario_id,
        "scenario_execution_id": request.scenario.scenario_execution_id,
    }
    mismatched = [
        field for field, value in expected.items() if receipt.get(field) != value
    ]
    owner_result = receipt.get("owner_result")
    if owner_result not in {
        "passed",
        "failed",
        "blocked",
        "cancelled",
        "denied",
        "unavailable",
    }:
        mismatched.append("owner_result")
    owner_execution = receipt.get("owner_execution")
    if not isinstance(owner_execution, Mapping):
        mismatched.append("owner_execution")
    else:
        _require_exact_fields(
            owner_execution,
            {"execution_type", "execution_id"},
            "owner receipt owner_execution",
        )
        if owner_execution != {
            "execution_type": "activity",
            "execution_id": request.activity_id,
        }:
            mismatched.append("owner_execution")

    recorded_at = _require_timestamp(
        receipt.get("recorded_at"),
        "owner receipt recorded_at",
    )
    if recorded_at < request.started_at:
        mismatched.append("recorded_at")
    if (
        owner_result == "passed"
        and recorded_at >= request.owner_context.authorization_expires_at
    ):
        mismatched.append("recorded_at")
    receipt_key = _owner_receipt_key(request)
    if receipt.get("receipt_ref") != f"wgcf-controlled-proof://receipts/{receipt_key}":
        mismatched.append("receipt_ref")
    receipt_without_digest = {
        key: value for key, value in receipt.items() if key != "receipt_digest"
    }
    if receipt.get("receipt_digest") != _record_digest(receipt_without_digest):
        mismatched.append("receipt_digest")

    observation_path = evidence_root / "observations" / f"{receipt_key}.json"
    observation = _load_json(observation_path)
    if observation is None:
        mismatched.append("observation")
        observation = {}
    stored_activity_result = _load_json(
        evidence_root / "activity-results" / f"{receipt_key}.json",
    )
    has_result_fields = {
        "activity_result_digest",
        "activity_status_code",
    }.issubset(observation)
    if bool(stored_activity_result is not None) != has_result_fields:
        mismatched.append("activity_result")
    observation_fields = {
        "schema_version",
        "owner_repo",
        "authorization_id",
        "authorization_digest",
        "commissioning_session_id",
        "scenario_id",
        "scenario_execution_id",
        "activity_id",
        "workflow_id",
        "workflow_run_id",
        "observation_kind",
        "owner_result",
        "process_group_fenced",
        "recorded_at",
    }
    if has_result_fields:
        observation_fields.update(
            {"activity_result_digest", "activity_status_code"},
        )
    if observation:
        _require_exact_fields(observation, observation_fields, "owner observation")
        expected_observation = {
            "schema_version": CONTROLLED_PROOF_RECEIPT_SCHEMA_VERSION,
            "owner_repo": CONTROLLED_PROOF_OWNER_REPO,
            "authorization_id": request.owner_context.authorization_id,
            "authorization_digest": request.owner_context.authorization_digest,
            "commissioning_session_id": (
                request.owner_context.commissioning_session_id
            ),
            "scenario_id": request.scenario.scenario_id,
            "scenario_execution_id": request.scenario.scenario_execution_id,
            "activity_id": request.activity_id,
            "workflow_id": request.workflow_id,
            "workflow_run_id": request.workflow_run_id,
            "owner_result": owner_result,
            "process_group_fenced": True,
            "recorded_at": receipt.get("recorded_at"),
        }
        if any(
            observation.get(field) != value
            for field, value in expected_observation.items()
        ):
            mismatched.append("observation")
    if stored_activity_result is not None:
        normalized_stored_result = _normalize_activity_result(
            stored_activity_result,
        )
        assert normalized_stored_result is not None
        if observation.get("activity_result_digest") != _record_digest(
            normalized_stored_result,
        ):
            mismatched.append("activity_result_digest")
        if observation.get("activity_status_code") != normalized_stored_result.get(
            "status_code",
        ):
            mismatched.append("activity_status_code")
        stored_activity_result = normalized_stored_result

    expected_evidence_refs = _owner_receipt_evidence_refs(
        request,
        receipt_key=receipt_key,
        observation_digest=_record_digest(observation),
        activity_result=stored_activity_result,
    )
    if receipt.get("evidence_refs") != expected_evidence_refs:
        mismatched.append("evidence_refs")
    if mismatched:
        raise ControlledProofContextMismatch(
            "existing controlled-proof receipt does not match: "
            + ", ".join(sorted(set(mismatched))),
        )
    return dict(observation), stored_activity_result


def _owner_receipt_evidence_refs(
    request: AuthorizedControlledProofRequest,
    *,
    receipt_key: str,
    observation_digest: str,
    activity_result: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    evidence_refs = [
        {
            "artifact_ref": request.owner_context.owner_context_id,
            "artifact_digest": request.owner_context.owner_context_digest,
        },
        {
            "artifact_ref": request.owner_context.orchestration_context_id,
            "artifact_digest": request.owner_context.orchestration_context_digest,
        },
        {
            "artifact_ref": request.owner_context.authorization_id,
            "artifact_digest": request.owner_context.authorization_digest,
        },
        {
            "artifact_ref": request.owner_context.operator_approval_ref,
            "artifact_digest": request.owner_context.operator_approval_digest,
        },
        {
            "artifact_ref": request.owner_context.security_authorization_ref,
            "artifact_digest": request.owner_context.security_authorization_digest,
        },
        {
            "artifact_ref": request.owner_context.consumption_receipt_ref,
            "artifact_digest": request.owner_context.consumption_receipt_digest,
        },
        {
            "artifact_ref": f"wgcf-controlled-proof://observations/{receipt_key}",
            "artifact_digest": observation_digest,
        },
    ]
    if activity_result is None:
        return evidence_refs
    evidence_refs.append(
        {
            "artifact_ref": f"wgcf-controlled-proof://activity-results/{receipt_key}",
            "artifact_digest": _record_digest(activity_result),
        },
    )
    receipt_ref = activity_result.get("receipt_ref")
    _require_mapping(receipt_ref, "activity_result.receipt_ref")
    assert isinstance(receipt_ref, Mapping)
    receipt_id = _require_identifier(
        receipt_ref.get("receipt_id"),
        "activity receipt_id",
    )
    receipt_digest = _require_digest(
        receipt_ref.get("digest"),
        "activity receipt digest",
    )
    evidence_refs.append(
        {
            "artifact_ref": (
                "wgcf-controlled-proof://validation-receipts/"
                f"{sha256(receipt_id.encode('utf-8')).hexdigest()}"
            ),
            "artifact_digest": receipt_digest,
        },
    )
    return evidence_refs


def _normalize_activity_result(
    activity_result: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if activity_result is None:
        return None
    _require_mapping(activity_result, "activity_result")
    try:
        normalized = _plain_record(activity_result)
    except (TypeError, ValueError) as exc:
        raise ControlledProofContractError(
            "activity_result must be a JSON object",
        ) from exc
    _require_identifier(normalized.get("status_code"), "activity_result.status_code")
    return normalized


def _owner_receipt_key(request: AuthorizedControlledProofRequest) -> str:
    binding = "\0".join(
        (
            request.owner_context.authorization_digest,
            request.owner_context.commissioning_session_id,
            request.scenario.scenario_execution_id,
            request.activity_id,
        ),
    )
    return sha256(binding.encode("utf-8")).hexdigest()


def _scenario_binding_key(request: AuthorizedControlledProofRequest) -> str:
    binding = "\0".join(
        (
            request.owner_context.authorization_digest,
            request.owner_context.commissioning_session_id,
            request.scenario.scenario_execution_id,
        ),
    )
    return sha256(binding.encode("utf-8")).hexdigest()


def _required_object(
    record: Mapping[str, Any],
    field: str,
    fields: set[str],
) -> Mapping[str, Any]:
    value = record.get(field)
    _require_mapping(value, field)
    assert isinstance(value, Mapping)
    _require_exact_fields(value, fields, field)
    return value


def _require_mapping(value: Any, field: str) -> None:
    if not isinstance(value, Mapping):
        raise ControlledProofContractError(f"{field} must be an object")


def _require_exact_fields(
    record: Mapping[str, Any],
    fields: set[str] | frozenset[str],
    label: str,
) -> None:
    unknown = sorted(set(record) - fields)
    missing = sorted(fields - set(record))
    if unknown:
        raise ControlledProofContractError(
            f"{label} contains unknown fields: {', '.join(unknown)}",
        )
    if missing:
        raise ControlledProofContractError(
            f"{label} is missing required fields: {', '.join(missing)}",
        )


def _require_equal(actual: Any, expected: Any, field: str) -> None:
    if actual != expected:
        raise ControlledProofContextMismatch(f"{field} does not match")


def _require_identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ControlledProofContractError(f"{field} must be a bounded identifier")
    return value


def _require_uri(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _URI_PATTERN.fullmatch(value):
        raise ControlledProofContractError(f"{field} must be a bounded URI")
    return value


def _require_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value):
        raise ControlledProofContractError(f"{field} must be a sha256 digest")
    return value


def _require_revision(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _REVISION_PATTERN.fullmatch(value):
        raise ControlledProofContractError(f"{field} must be a full source revision")
    return value


def _require_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ControlledProofContractError(f"{field} must be a UTC RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ControlledProofContractError(
            f"{field} must be a UTC RFC 3339 timestamp",
        ) from exc
    return _as_utc(parsed, field)


def _as_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ControlledProofContractError(f"{field} must be timezone aware")
    return value.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return _as_utc(value, "timestamp").isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )


def _require_receipt_owners(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ControlledProofContractError(f"{field} must be an owner list")
    owners = tuple(value)
    if not 1 <= len(owners) <= len(CONTROLLED_PROOF_RECEIPT_OWNERS):
        raise ControlledProofContractError(f"{field} must contain 1 to 3 owners")
    if any(
        not isinstance(owner, str)
        or owner not in CONTROLLED_PROOF_RECEIPT_OWNERS
        for owner in owners
    ):
        raise ControlledProofContractError(f"{field} contains an unsupported owner")
    if len(set(owners)) != len(owners):
        raise ControlledProofContractError(f"{field} must contain unique owners")
    return owners


def _record_digest(record: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        record,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    ).encode("utf-8")
    return f"sha256:{sha256(encoded).hexdigest()}"


def _plain_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(record, separators=(",", ":"), sort_keys=True))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise ControlledProofContractError(
                f"controlled-proof context contains duplicate field: {key}",
            )
        record[key] = value
    return record


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        record = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (
        ControlledProofContractError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise ControlledProofContextMismatch(
            f"controlled-proof evidence is unreadable: {path.name}",
        ) from exc
    if not isinstance(record, dict):
        raise ControlledProofContextMismatch(
            f"controlled-proof evidence is not an object: {path.name}",
        )
    return record


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_json_atomic(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _write_or_verify_json(
    path: Path,
    record: Mapping[str, Any],
    label: str,
) -> None:
    existing = _load_json(path)
    if existing is not None:
        if existing != record:
            raise ControlledProofContextMismatch(
                f"existing {label} does not match the authorized execution",
            )
        return
    _write_json_atomic(path, record)
