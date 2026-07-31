"""Bounded orchestration activities owned by the control fabric.

The aggregate workflow remains owned by Operator Orchestration Service. This
module exposes only the idempotent validation/readiness activity that WGCF is
authorized to execute and records only fabric-local evidence.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from asyncio import CancelledError
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator, Mapping

from .operator_surfaces import (
    run_catalog_operator_validation_check,
    run_operator_readiness_evaluation,
)


VALIDATION_READINESS_ACTIVITY_NAME = "wgcf.validation-readiness.evaluate"
VALIDATION_READINESS_TASK_QUEUE = "wgcf.validation-readiness.v1"
VALIDATION_READINESS_DEFINITION_ID = "validation-readiness-run"
VALIDATION_READINESS_DEFINITION_VERSION = 1
VALIDATION_READINESS_SCHEMA_VERSION = 1
VALIDATION_READINESS_CALLER_ID = "operator-orchestration-service"
VALIDATION_READINESS_PROFILE = "local-read-only"
VALIDATION_READINESS_TIER = "smoke"
VALIDATION_READINESS_SCOPE = "component:workspace-governance"
VALIDATION_READINESS_TARGET = "repo:workspace-governance-control-fabric"
VALIDATION_READINESS_RESULT_STATUS_CODES = (
    "ready",
    "blocked",
    "timed-out",
    "unavailable",
)
VALIDATION_READINESS_FAILURE_STATUS_CODES = (
    "blocked",
    "retryable",
    "timed-out",
    "cancelled",
    "unavailable",
)

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_REQUEST_FIELDS = frozenset(
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
    },
)


class ValidationReadinessContractError(ValueError):
    """Raised when an activity request crosses the declared owner boundary."""


class ValidationReadinessIdempotencyConflict(ValidationReadinessContractError):
    """Raised when one idempotency key is reused for a different request."""


@dataclass(frozen=True)
class ValidationReadinessFailureClassification:
    """Bounded failure projection used by the Temporal adapter."""

    error_type: str
    public_message: str
    retryable: bool
    status_code: str


@dataclass(frozen=True)
class ValidationReadinessActivityRequest:
    """Strict input accepted by the validation/readiness activity."""

    schema_version: int
    definition_id: str
    definition_version: int
    run_id: str
    workflow_id: str
    source_ref: str
    source_version: str
    validation_scope: str
    readiness_target: str
    profile: str
    tier: str
    correlation_id: str
    causation_id: str
    idempotency_key: str
    caller_id: str
    operator_id: str

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
    ) -> "ValidationReadinessActivityRequest":
        if not isinstance(payload, Mapping):
            raise ValidationReadinessContractError("activity payload must be an object")

        unknown = sorted(set(payload) - _REQUEST_FIELDS)
        missing = sorted(_REQUEST_FIELDS - set(payload))
        if unknown:
            raise ValidationReadinessContractError(
                f"activity payload contains unknown fields: {', '.join(unknown)}",
            )
        if missing:
            raise ValidationReadinessContractError(
                f"activity payload is missing required fields: {', '.join(missing)}",
            )

        schema_version = _required_integer(payload, "schema_version")
        definition_version = _required_integer(payload, "definition_version")
        request = cls(
            schema_version=schema_version,
            definition_id=_required_identifier(payload, "definition_id"),
            definition_version=definition_version,
            run_id=_required_identifier(payload, "run_id"),
            workflow_id=_required_identifier(payload, "workflow_id"),
            source_ref=_required_identifier(payload, "source_ref"),
            source_version=_required_identifier(payload, "source_version"),
            validation_scope=_required_identifier(payload, "validation_scope"),
            readiness_target=_required_identifier(payload, "readiness_target"),
            profile=_required_identifier(payload, "profile"),
            tier=_required_identifier(payload, "tier"),
            correlation_id=_required_identifier(payload, "correlation_id"),
            causation_id=_required_identifier(payload, "causation_id"),
            idempotency_key=_required_identifier(payload, "idempotency_key"),
            caller_id=_required_identifier(payload, "caller_id"),
            operator_id=_required_identifier(payload, "operator_id"),
        )
        request._validate_boundary()
        return request

    def _validate_boundary(self) -> None:
        expected = {
            "schema_version": (
                self.schema_version,
                VALIDATION_READINESS_SCHEMA_VERSION,
            ),
            "definition_id": (
                self.definition_id,
                VALIDATION_READINESS_DEFINITION_ID,
            ),
            "definition_version": (
                self.definition_version,
                VALIDATION_READINESS_DEFINITION_VERSION,
            ),
            "validation_scope": (
                self.validation_scope,
                VALIDATION_READINESS_SCOPE,
            ),
            "readiness_target": (
                self.readiness_target,
                VALIDATION_READINESS_TARGET,
            ),
            "profile": (self.profile, VALIDATION_READINESS_PROFILE),
            "tier": (self.tier, VALIDATION_READINESS_TIER),
            "caller_id": (self.caller_id, VALIDATION_READINESS_CALLER_ID),
        }
        violations = [
            f"{field} must be {required!r}"
            for field, (actual, required) in expected.items()
            if actual != required
        ]
        if violations:
            raise ValidationReadinessContractError("; ".join(violations))

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationReadinessActivityContext:
    """Temporal execution metadata that is safe to include in evidence."""

    activity_id: str
    attempt: int
    worker_id: str
    workflow_id: str

    def __post_init__(self) -> None:
        _validate_identifier("activity_id", self.activity_id)
        _validate_identifier("worker_id", self.worker_id)
        _validate_identifier("workflow_id", self.workflow_id)
        if isinstance(self.attempt, bool) or self.attempt < 1:
            raise ValidationReadinessContractError("attempt must be an integer of at least 1")


def execute_validation_readiness_activity(
    payload: Mapping[str, Any],
    *,
    activity_context: ValidationReadinessActivityContext,
    evidence_root: str | Path,
    repo_root: str | Path,
    workspace_root: str | Path,
) -> dict[str, Any]:
    """Execute the bounded activity once per idempotency key.

    The returned payload deliberately omits local paths, raw validator output,
    and authority records. Those stay in WGCF-owned evidence storage.
    """

    request, request_digest, key_digest = _validation_readiness_binding(payload)
    if request.workflow_id != activity_context.workflow_id:
        raise ValidationReadinessContractError(
            "workflow_id must match the Temporal execution context",
        )
    evidence_path = Path(evidence_root).resolve()
    cache_path = evidence_path / "idempotency" / f"{key_digest}.json"
    lock_path = evidence_path / "locks" / f"{key_digest}.lock"

    with _exclusive_lock(lock_path):
        cached = _load_cached_result(cache_path)
        if cached is not None:
            if cached["request_digest"] != request_digest:
                raise ValidationReadinessIdempotencyConflict(
                    "idempotency key is already bound to a different request",
                )
            return dict(cached["result"])

        run_root = evidence_path / "runs" / key_digest[:24]
        actor = f"orchestration:{request.caller_id}:{request.operator_id}"
        validation = run_catalog_operator_validation_check(
            workspace_root=workspace_root,
            target_scope=request.validation_scope,
            artifact_root=run_root / "artifacts",
            receipt_dir=run_root / "receipts",
            ledger_path=run_root / "ledger.jsonl",
            manifest_dir=run_root / "manifests",
            actor=actor,
            operator_approved=False,
            profile=request.profile,
            tier=request.tier,
        )
        readiness = run_operator_readiness_evaluation(
            actor=actor,
            ledger_path=run_root / "ledger.jsonl",
            profile=request.profile,
            receipt_dir=run_root / "receipts",
            repo_root=repo_root,
            target=request.readiness_target,
        )
        result = _build_result(
            activity_context=activity_context,
            readiness=readiness,
            request=request,
            validation=validation,
        )
        _write_json_atomic(
            cache_path,
            {
                "schema_version": VALIDATION_READINESS_SCHEMA_VERSION,
                "request_digest": request_digest,
                "result": result,
            },
        )
        return result


def load_committed_validation_readiness_result(
    payload: Mapping[str, Any],
    *,
    evidence_root: str | Path,
) -> dict[str, Any] | None:
    """Read one canonically committed result without executing owner work."""

    _request, request_digest, key_digest = _validation_readiness_binding(payload)
    evidence_path = Path(evidence_root).resolve()
    lock_path = evidence_path / "locks" / f"{key_digest}.lock"
    committed_root = evidence_path / "committed" / key_digest
    cache_path = committed_root / "idempotency" / f"{key_digest}.json"
    with _exclusive_lock(lock_path):
        return _bound_cached_result(cache_path, request_digest)


def commit_validation_readiness_staging_result(
    payload: Mapping[str, Any],
    *,
    evidence_root: str | Path,
    staging_root: str | Path,
    expected_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically grant canonical evidence authority to one stopped attempt."""

    _request, request_digest, key_digest = _validation_readiness_binding(payload)
    evidence_path = Path(evidence_root).resolve()
    staging_path = Path(staging_root).resolve()
    staging_parent = (evidence_path / "staging").resolve()
    if staging_path.parent != staging_parent:
        raise RuntimeError("validation/readiness staging root is outside its authority boundary")

    staged_cache_path = staging_path / "idempotency" / f"{key_digest}.json"
    staged_result = _bound_cached_result(staged_cache_path, request_digest)
    if staged_result is None:
        raise RuntimeError("validation/readiness staging result is missing")
    if staged_result != dict(expected_result):
        raise RuntimeError("validation/readiness staging result does not match owner output")

    lock_path = evidence_path / "locks" / f"{key_digest}.lock"
    committed_root = evidence_path / "committed" / key_digest
    committed_cache_path = committed_root / "idempotency" / f"{key_digest}.json"
    with _exclusive_lock(lock_path):
        committed_result = _bound_cached_result(
            committed_cache_path,
            request_digest,
        )
        if committed_result is not None:
            return committed_result
        if committed_root.exists():
            raise RuntimeError("validation/readiness committed evidence is incomplete")
        committed_root.parent.mkdir(parents=True, exist_ok=True)
        staging_path.rename(committed_root)
        return staged_result


def classify_validation_readiness_exception(
    error: BaseException,
) -> ValidationReadinessFailureClassification:
    """Map failures to stable Temporal semantics without exposing raw details."""

    if isinstance(error, ValidationReadinessIdempotencyConflict):
        return ValidationReadinessFailureClassification(
            error_type="WGCF_IDEMPOTENCY_CONFLICT",
            public_message="WGCF rejected an idempotency key collision",
            retryable=False,
            status_code="blocked",
        )
    if isinstance(error, ValidationReadinessContractError):
        return ValidationReadinessFailureClassification(
            error_type="WGCF_CONTRACT_REJECTED",
            public_message="WGCF rejected the bounded activity contract",
            retryable=False,
            status_code="blocked",
        )
    if isinstance(error, CancelledError):
        return ValidationReadinessFailureClassification(
            error_type="WGCF_ACTIVITY_CANCELLED",
            public_message="WGCF activity execution was cancelled",
            retryable=False,
            status_code="cancelled",
        )
    if isinstance(error, TimeoutError):
        return ValidationReadinessFailureClassification(
            error_type="WGCF_ACTIVITY_TIMED_OUT",
            public_message="WGCF activity execution timed out before producing a result",
            retryable=True,
            status_code="timed-out",
        )
    if isinstance(error, OSError):
        return ValidationReadinessFailureClassification(
            error_type="WGCF_ACTIVITY_UNAVAILABLE",
            public_message="WGCF activity execution is temporarily unavailable",
            retryable=True,
            status_code="unavailable",
        )
    return ValidationReadinessFailureClassification(
        error_type="WGCF_ACTIVITY_RETRYABLE",
        public_message="WGCF activity execution failed before producing a result",
        retryable=True,
        status_code="retryable",
    )


def _build_result(
    *,
    activity_context: ValidationReadinessActivityContext,
    readiness: Any,
    request: ValidationReadinessActivityRequest,
    validation: Any,
) -> dict[str, Any]:
    receipt = validation.receipt
    decision = readiness.decision
    validation_outcome = str(receipt.outcome)
    readiness_outcome = str(decision.outcome)
    status_code = _result_status_code(receipt, decision)
    ready = status_code == "ready"
    evidence_digest = _record_digest(
        {
            "readiness_decision_ref": decision.decision_id,
            "readiness_event_ref": readiness.ledger_event.event_id,
            "validation_event_ref": validation.check_result.ledger_event.event_id,
            "validation_receipt_digest": receipt.digest,
        },
    )
    return {
        "schema_version": VALIDATION_READINESS_SCHEMA_VERSION,
        "activity_name": VALIDATION_READINESS_ACTIVITY_NAME,
        "activity_id": activity_context.activity_id,
        "attempt": activity_context.attempt,
        "worker_id": activity_context.worker_id,
        "definition_id": request.definition_id,
        "definition_version": request.definition_version,
        "run_id": request.run_id,
        "workflow_id": request.workflow_id,
        "source_ref": request.source_ref,
        "source_version": request.source_version,
        "correlation_id": request.correlation_id,
        "causation_id": request.causation_id,
        "idempotency_key": request.idempotency_key,
        "status_code": status_code,
        "bounded_decision": {
            "ready": ready,
            "terminal": True,
            "retryable": False,
            "validation_outcome": validation_outcome,
            "readiness_outcome": readiness_outcome,
            "readiness_reason_count": len(decision.reasons),
            "readiness_decision_ref": decision.decision_id,
            "validation_event_ref": validation.check_result.ledger_event.event_id,
            "readiness_event_ref": readiness.ledger_event.event_id,
        },
        "artifact_digest": evidence_digest,
        "receipt_ref": {
            "receipt_id": receipt.receipt_id,
            "digest": receipt.digest,
            "outcome": receipt.outcome,
            "target_scope": receipt.target_scope,
            "tier": receipt.tier,
        },
    }


def _result_status_code(receipt: Any, decision: Any) -> str:
    check_results = tuple(getattr(receipt, "check_results", ()) or ())
    if any(
        bool(getattr(check, "output_summary", {}).get("timed_out"))
        for check in check_results
    ):
        return "timed-out"
    if any(
        getattr(check, "error", None) and getattr(check, "exit_code", None) is None
        for check in check_results
    ):
        return "unavailable"
    if str(receipt.outcome) != "success" or not bool(decision.ready):
        return "blocked"
    return "ready"


def _required_integer(payload: Mapping[str, Any], field: str) -> int:
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationReadinessContractError(f"{field} must be an integer")
    return value


def _required_identifier(payload: Mapping[str, Any], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise ValidationReadinessContractError(f"{field} must be a string")
    _validate_identifier(field, value)
    return value


def _validate_identifier(field: str, value: str) -> None:
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValidationReadinessContractError(
            f"{field} must be a bounded identifier without whitespace or control characters",
        )


def _record_digest(record: Mapping[str, Any]) -> str:
    digest = sha256(
        json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8"),
    ).hexdigest()
    return f"sha256:{digest}"


def _validation_readiness_binding(
    payload: Mapping[str, Any],
) -> tuple[ValidationReadinessActivityRequest, str, str]:
    request = ValidationReadinessActivityRequest.from_payload(payload)
    request_digest = _record_digest(request.to_record())
    key_digest = sha256(request.idempotency_key.encode("utf-8")).hexdigest()
    return request, request_digest, key_digest


def _bound_cached_result(
    cache_path: Path,
    request_digest: str,
) -> dict[str, Any] | None:
    cached = _load_cached_result(cache_path)
    if cached is None:
        return None
    if cached["request_digest"] != request_digest:
        raise ValidationReadinessIdempotencyConflict(
            "idempotency key is already bound to a different request",
        )
    return dict(cached["result"])


def _load_cached_result(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"idempotency record is unreadable: {path.name}") from exc
    if not isinstance(record, dict):
        raise RuntimeError(f"idempotency record is not an object: {path.name}")
    if not isinstance(record.get("request_digest"), str):
        raise RuntimeError(f"idempotency record has no request digest: {path.name}")
    if not isinstance(record.get("result"), dict):
        raise RuntimeError(f"idempotency record has no result object: {path.name}")
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
