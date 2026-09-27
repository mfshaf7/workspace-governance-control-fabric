"""Bounded, read-only governance-history projections for operator consumers."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, datetime
import hmac
import json
import os
from typing import Any, Callable, Iterable

from sqlalchemy import Select, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .database import create_session_factory
from .db.models import (
    ControlReceipt,
    DeliveryArtReadinessReceipt,
    EscalationRecord,
    LedgerEvent,
    LifecycleTransitionReadinessRecord,
    PrototypeClosureReadinessRecord,
    PrototypeIngressReadinessReceipt,
    PrototypeLandingReadinessRecord,
    PrototypeMaturityReadinessRecord,
    ReadinessDecision,
    RepositoryCustodyDecisionRecord,
    RepositoryLifecycleDecisionRecord,
    RepositoryReadinessReceipt,
    WorkspaceIntakeEvaluationRecord,
    WorkspaceInventoryEvaluationRecord,
    WorkspaceInventoryLifecycleEvaluationRecord,
)


HISTORY_CALLER_ID_ENV = "WGCF_GOVERNANCE_HISTORY_CALLER_ID"
HISTORY_CALLER_SECRET_ENV = "WGCF_GOVERNANCE_HISTORY_CALLER_SECRET"
DEFAULT_HISTORY_CALLER_ID = "governance-operations-console"
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
MAX_SOURCE_SCAN = 500
AUTHORITY_BOUNDARY = {
    "owner_repo": "workspace-governance-control-fabric",
    "role": "runtime-evidence-projection",
    "approval_source": False,
}
SAFE_REFERENCE_PREFIXES = (
    "artifact://",
    "delivery-art://",
    "github://",
    "https://github.com/",
    "oos://",
    "openproject://",
    "platform://",
    "prototype://",
    "receipt://",
    "review-packet://",
    "security://",
    "wgcf://",
    "workspace-governance://",
)


class GovernanceHistoryError(RuntimeError):
    """Base error for governance-history projections."""


class GovernanceHistoryUnauthorized(GovernanceHistoryError):
    """The history caller could not be authenticated."""


class GovernanceHistoryUnavailable(GovernanceHistoryError):
    """The history store is unavailable."""


class GovernanceHistoryNotFound(GovernanceHistoryError):
    """The requested history record does not exist."""


class GovernanceHistoryRequestError(GovernanceHistoryError):
    """The history request is malformed."""


class GovernanceHistoryAuthorizer:
    """Authenticate one dedicated read-only history consumer."""

    def __init__(self, *, caller_id: str, caller_secret: str) -> None:
        if not caller_id.strip() or len(caller_secret) < 32:
            raise GovernanceHistoryUnavailable(
                "governance-history caller authentication is not configured",
            )
        self._caller_id = caller_id.strip()
        self._caller_secret = caller_secret

    @classmethod
    def from_environment(cls) -> GovernanceHistoryAuthorizer:
        return cls(
            caller_id=os.environ.get(HISTORY_CALLER_ID_ENV, DEFAULT_HISTORY_CALLER_ID),
            caller_secret=os.environ.get(HISTORY_CALLER_SECRET_ENV, ""),
        )

    def authorize(self, caller_id: str, caller_secret: str) -> None:
        if (
            not caller_secret
            or caller_id != self._caller_id
            or not hmac.compare_digest(caller_secret, self._caller_secret)
        ):
            raise GovernanceHistoryUnauthorized(
                "governance-history caller authentication failed",
            )


@dataclass(frozen=True)
class _Source:
    source_id: str
    category: str
    model: type[Any]
    id_attr: str
    time_attr: str
    projector: Callable[[Any, "_Source"], dict[str, Any]]


def _specific_readiness(row: Any, source: _Source) -> dict[str, Any]:
    payload = _payload(row)
    native_id = str(getattr(row, source.id_attr))
    subject = _specific_subject(row, payload, source.source_id)
    outcome = _specific_outcome(row, payload)
    action = _specific_action(row, source.source_id)
    return _projection(
        source=source,
        native_id=native_id,
        occurred_at=getattr(row, source.time_attr),
        actor=_string_value(getattr(row, "actor", None)) or _string_from(payload, "actor"),
        action=action,
        subject=subject,
        outcome=outcome,
        freshness=_freshness(row, payload),
        evidence_routes=_safe_references(row, payload),
        next_action=_next_action(payload),
        metadata=_metadata(
            row,
            (
                "profile_id",
                "generation",
                "implementation_ref",
                "authority_revision",
                "contract_digest",
                "policy_version",
                "source_revision",
                "transition_id",
                "delivery_id",
                "repo_name",
                "provider",
                "provider_repository_id",
                "action",
            ),
        ),
    )


def _control_receipt(row: ControlReceipt, source: _Source) -> dict[str, Any]:
    return _projection(
        source=source,
        native_id=row.receipt_id,
        occurred_at=row.created_at,
        action="control-receipt-recorded",
        subject=row.target,
        outcome=row.outcome,
        freshness="not-applicable",
        evidence_routes=_safe_references(row, {"artifact_refs": row.artifact_refs}),
        next_action=_bounded_action(row.next_required_action),
        metadata={
            "profile_id": row.profile_id,
            "finding_count": len(row.findings),
            "artifact_count": len(row.artifact_refs),
        },
    )


def _readiness_decision(row: ReadinessDecision, source: _Source) -> dict[str, Any]:
    payload = {
        "authority_refs": row.authority_refs,
        "receipt_refs": row.receipt_refs,
    }
    return _projection(
        source=source,
        native_id=row.decision_id,
        occurred_at=row.created_at,
        action="readiness-decision-recorded",
        subject=row.target,
        outcome=row.outcome,
        freshness="current",
        evidence_routes=_safe_references(row, payload),
        next_action=_bounded_action(row.escalation_path_when_blocked),
        metadata={
            "profile_id": row.profile_id,
            "reason_count": len(row.reasons),
            "receipt_count": len(row.receipt_refs),
        },
    )


def _ledger_event(row: LedgerEvent, source: _Source) -> dict[str, Any]:
    return _projection(
        source=source,
        native_id=row.event_id,
        occurred_at=row.event_time,
        actor=row.actor,
        action=row.action,
        subject=row.target,
        outcome=row.outcome,
        freshness="not-applicable",
        evidence_routes=_safe_references(row, {"receipt_refs": row.receipt_refs}),
        next_action=None,
        metadata={
            "source_snapshot_id": row.source_snapshot_id,
            "receipt_count": len(row.receipt_refs),
        },
    )


def _escalation(row: EscalationRecord, source: _Source) -> dict[str, Any]:
    return _projection(
        source=source,
        native_id=row.escalation_id,
        occurred_at=row.created_at,
        action="governance-escalation-recorded",
        subject=row.trigger_id,
        outcome="action-required",
        freshness="current",
        evidence_routes=_safe_references(row, {"evidence_refs": row.evidence_refs}),
        next_action={
            "authority": row.owner_repo,
            "route": row.target_system,
            "action": _bounded_text(row.operator_action_required),
        },
        metadata={
            "owner_repo": row.owner_repo,
            "target_system": row.target_system,
            "required_record": row.required_record,
            "evidence_count": len(row.evidence_refs),
        },
    )


SOURCES = (
    _Source("readiness-decisions", "readiness", ReadinessDecision, "decision_id", "created_at", _readiness_decision),
    _Source(
        "lifecycle-transition-readiness",
        "readiness",
        LifecycleTransitionReadinessRecord,
        "evaluation_id",
        "created_at",
        _specific_readiness,
    ),
    _Source(
        "delivery-art-readiness",
        "readiness",
        DeliveryArtReadinessReceipt,
        "receipt_id",
        "evaluated_at",
        _specific_readiness,
    ),
    _Source(
        "prototype-ingress-readiness",
        "readiness",
        PrototypeIngressReadinessReceipt,
        "receipt_id",
        "evaluated_at",
        _specific_readiness,
    ),
    _Source(
        "repository-readiness",
        "readiness",
        RepositoryReadinessReceipt,
        "receipt_id",
        "evaluated_at",
        _specific_readiness,
    ),
    _Source(
        "repository-custody",
        "readiness",
        RepositoryCustodyDecisionRecord,
        "decision_id",
        "evaluated_at",
        _specific_readiness,
    ),
    _Source(
        "repository-lifecycle",
        "readiness",
        RepositoryLifecycleDecisionRecord,
        "decision_id",
        "evaluated_at",
        _specific_readiness,
    ),
    _Source(
        "workspace-intake",
        "readiness",
        WorkspaceIntakeEvaluationRecord,
        "evaluation_id",
        "created_at",
        _specific_readiness,
    ),
    _Source(
        "workspace-inventory",
        "readiness",
        WorkspaceInventoryEvaluationRecord,
        "evaluation_id",
        "created_at",
        _specific_readiness,
    ),
    _Source(
        "workspace-inventory-lifecycle",
        "readiness",
        WorkspaceInventoryLifecycleEvaluationRecord,
        "evaluation_id",
        "created_at",
        _specific_readiness,
    ),
    _Source(
        "prototype-landing",
        "readiness",
        PrototypeLandingReadinessRecord,
        "evaluation_id",
        "created_at",
        _specific_readiness,
    ),
    _Source(
        "prototype-maturity",
        "readiness",
        PrototypeMaturityReadinessRecord,
        "evaluation_id",
        "created_at",
        _specific_readiness,
    ),
    _Source(
        "prototype-closure",
        "readiness",
        PrototypeClosureReadinessRecord,
        "evaluation_id",
        "created_at",
        _specific_readiness,
    ),
    _Source("control-receipts", "receipt", ControlReceipt, "receipt_id", "created_at", _control_receipt),
    _Source("ledger-events", "ledger", LedgerEvent, "event_id", "event_time", _ledger_event),
    _Source("escalations", "escalation", EscalationRecord, "escalation_id", "created_at", _escalation),
)
SOURCE_BY_ID = {source.source_id: source for source in SOURCES}
SUPPORTED_CATEGORIES = frozenset(source.category for source in SOURCES)


class GovernanceHistoryService:
    """Project existing WGCF runtime records without mutating their stores."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list(
        self,
        *,
        category: str | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        outcome: str | None = None,
        subject: str | None = None,
    ) -> dict[str, Any]:
        normalized_category = _optional_filter(category, "category")
        normalized_outcome = _optional_filter(outcome, "outcome")
        normalized_subject = _optional_filter(subject, "subject")
        if normalized_category and normalized_category not in SUPPORTED_CATEGORIES:
            raise GovernanceHistoryRequestError("category is not supported")
        if limit < 1 or limit > MAX_PAGE_SIZE:
            raise GovernanceHistoryRequestError(
                f"limit must be between 1 and {MAX_PAGE_SIZE}",
            )
        cursor_key = _decode_cursor(cursor) if cursor else None
        source_scan = MAX_SOURCE_SCAN
        records: list[dict[str, Any]] = []
        statuses: list[dict[str, Any]] = []
        selected_sources = tuple(
            source for source in SOURCES
            if normalized_category is None or source.category == normalized_category
        )
        try:
            with self._session_factory() as session:
                for source in selected_sources:
                    projected, status = self._read_source(
                        session,
                        source,
                        cursor_key=cursor_key,
                        scan_limit=source_scan,
                    )
                    statuses.append(status)
                    records.extend(projected)
        except SQLAlchemyError as exc:
            raise GovernanceHistoryUnavailable(
                "governance-history storage is unavailable",
            ) from exc

        records.sort(key=_sort_key, reverse=True)
        page: list[dict[str, Any]] = []
        examined = 0
        boundary: dict[str, Any] | None = None
        for record in records:
            if examined >= MAX_SOURCE_SCAN or len(page) >= limit:
                break
            examined += 1
            boundary = record
            if _matches(record, outcome=normalized_outcome, subject=normalized_subject):
                page.append(record)
        has_more = (
            examined < len(records)
            or any(status["truncated"] for status in statuses)
        )
        next_cursor = _encode_cursor(boundary) if boundary and has_more else None
        partial = any(status["state"] != "available" for status in statuses)
        return {
            "schema_version": 1,
            "projection_state": "partial" if partial else "complete",
            "authority_boundary": dict(AUTHORITY_BOUNDARY),
            "filters": {
                "category": normalized_category,
                "outcome": normalized_outcome,
                "subject": normalized_subject,
            },
            "page": {
                "limit": limit,
                "returned": len(page),
                "has_more": has_more,
                "next_cursor": next_cursor,
            },
            "sources": statuses,
            "records": page,
        }

    def detail(self, history_id: str) -> dict[str, Any]:
        source_id, native_id = _decode_history_id(history_id)
        source = SOURCE_BY_ID.get(source_id)
        if source is None:
            raise GovernanceHistoryNotFound("governance-history record was not found")
        try:
            with self._session_factory() as session:
                row = session.get(source.model, native_id)
                if row is None:
                    raise GovernanceHistoryNotFound(
                        "governance-history record was not found",
                    )
                record = source.projector(row, source)
        except GovernanceHistoryNotFound:
            raise
        except SQLAlchemyError as exc:
            raise GovernanceHistoryUnavailable(
                "governance-history storage is unavailable",
            ) from exc
        return {
            "schema_version": 1,
            "projection_state": "complete",
            "authority_boundary": dict(AUTHORITY_BOUNDARY),
            "source": {
                "id": source.source_id,
                "category": source.category,
                "state": "available",
            },
            "record": record,
        }

    def _read_source(
        self,
        session: Session,
        source: _Source,
        *,
        cursor_key: tuple[str, str] | None,
        scan_limit: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        time_column = getattr(source.model, source.time_attr)
        id_column = getattr(source.model, source.id_attr)
        statement: Select[Any] = select(source.model).order_by(
            time_column.desc(),
            id_column.desc(),
        ).limit(scan_limit + 1)
        if cursor_key is not None:
            cursor_time = datetime.fromisoformat(cursor_key[0].replace("Z", "+00:00"))
            statement = statement.where(time_column <= cursor_time)
        try:
            rows = list(session.scalars(statement))
        except SQLAlchemyError:
            session.rollback()
            return [], {
                "id": source.source_id,
                "category": source.category,
                "state": "unavailable",
                "scanned": 0,
                "truncated": False,
                "reason_code": "storage-unavailable",
            }
        truncated = len(rows) > scan_limit
        projected = [source.projector(row, source) for row in rows[:scan_limit]]
        if cursor_key is not None:
            projected = [record for record in projected if _sort_key(record) < cursor_key]
        return projected, {
            "id": source.source_id,
            "category": source.category,
            "state": "available",
            "scanned": min(len(rows), scan_limit),
            "truncated": truncated,
            "reason_code": None,
        }


def build_governance_history_runtime(
    database_url: str | None = None,
) -> GovernanceHistoryService:
    return GovernanceHistoryService(create_session_factory(database_url))


def _projection(
    *,
    source: _Source,
    native_id: str,
    occurred_at: datetime,
    action: str,
    subject: str,
    outcome: str,
    freshness: str,
    evidence_routes: list[dict[str, str]],
    next_action: dict[str, Any] | None,
    metadata: dict[str, Any],
    actor: str | None = None,
) -> dict[str, Any]:
    timestamp = _timestamp(occurred_at)
    normalized_subject = _safe_subject(subject)
    normalized_outcome = _bounded_text(outcome) or "unknown"
    return {
        "history_id": _encode_history_id(source.source_id, native_id),
        "source": source.source_id,
        "category": source.category,
        "occurred_at": timestamp,
        "actor": _bounded_text(actor) if actor else None,
        "action": _bounded_text(action) or "recorded",
        "subject": normalized_subject,
        "outcome": normalized_outcome,
        "freshness": freshness,
        "summary": _bounded_text(
            f"{_bounded_text(action) or 'recorded'}: {normalized_subject} ({normalized_outcome})",
        ),
        "authority_boundary": dict(AUTHORITY_BOUNDARY),
        "evidence_routes": evidence_routes,
        "next_action": next_action,
        "metadata": {key: value for key, value in metadata.items() if value is not None},
    }


def _payload(row: Any) -> dict[str, Any]:
    for name in ("readiness", "receipt", "decision"):
        value = getattr(row, name, None)
        if isinstance(value, dict):
            return value
    return {}


def _specific_subject(row: Any, payload: dict[str, Any], source_id: str) -> str:
    direct = (
        "transition_id",
        "delivery_id",
        "repo_name",
        "provider_repository_id",
        "source_record_ref",
        "workspace_owner_ref",
    )
    for name in direct:
        value = _string_value(getattr(row, name, None))
        if value:
            return value
    for key in (
        "target",
        "subject",
        "prototype_id",
        "repo_name",
        "entrant_id",
        "component_id",
        "product_id",
        "source_record_ref",
    ):
        value = _string_from(payload, key)
        if value:
            return value
    return source_id


def _specific_outcome(row: Any, payload: dict[str, Any]) -> str:
    value = _string_value(getattr(row, "outcome", None))
    if value:
        return value
    for key in ("outcome", "status", "decision", "state"):
        value = _string_from(payload, key)
        if value:
            return value
    return "recorded"


def _specific_action(row: Any, source_id: str) -> str:
    value = _string_value(getattr(row, "action", None))
    return value or f"{source_id}-recorded"


def _freshness(row: Any, payload: dict[str, Any]) -> str:
    expires_at = getattr(row, "expires_at", None)
    if isinstance(expires_at, datetime):
        now = datetime.now(UTC)
        resolved = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        return "stale" if resolved <= now else "current"
    value = _string_from(payload, "freshness")
    if value in {"current", "stale", "unknown"}:
        return value
    nested = payload.get("freshness")
    if isinstance(nested, dict):
        state = _string_value(nested.get("state"))
        if state in {"current", "stale", "unknown"}:
            return state
    return "not-applicable"


def _metadata(row: Any, names: Iterable[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in names:
        value = getattr(row, name, None)
        if isinstance(value, (str, int, bool)):
            result[name] = value
    return result


def _safe_references(row: Any, payload: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[str] = []
    for name in ("receipt_uri", "decision_uri", "registry_uri"):
        value = getattr(row, name, None)
        if isinstance(value, str):
            candidates.append(value)
    _collect_reference_candidates(payload, candidates)
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in candidates:
        ref = value.strip()
        if ref in seen or not ref.startswith(SAFE_REFERENCE_PREFIXES):
            continue
        seen.add(ref)
        result.append({"ref": ref, "route_type": ref.split(":", 1)[0]})
    return result[:32]


def _collect_reference_candidates(value: Any, result: list[str], key: str = "") -> None:
    lowered = key.lower()
    if any(fragment in lowered for fragment in ("secret", "token", "password", "private_key")):
        return
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            _collect_reference_candidates(child_value, result, str(child_key))
    elif isinstance(value, list):
        for child in value:
            _collect_reference_candidates(child, result, key)
    elif isinstance(value, str) and ("ref" in lowered or lowered.endswith("uri")):
        result.append(value)


def _next_action(payload: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("next_action", "required_action", "operator_action"):
        value = payload.get(key)
        if isinstance(value, dict):
            return _bounded_action(value)
    return None


def _bounded_action(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value:
        return None
    result: dict[str, Any] = {}
    for key in ("authority", "code", "route", "owner_repo", "action", "reason"):
        text = _string_value(value.get(key))
        if text:
            result[key] = _bounded_text(text)
    return result or None


def _string_from(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, dict):
        for nested_key in ("id", "ref", "state", "outcome", "name"):
            nested = _string_value(value.get(nested_key))
            if nested:
                return nested
        return None
    return _string_value(value)


def _string_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _bounded_text(value: str | None, limit: int = 512) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized[:limit]


def _safe_subject(value: str | None) -> str:
    normalized = _bounded_text(value) or "unknown"
    if normalized.startswith(("/", "./", "../", "file://", "env://", "vault://")):
        return "restricted-subject"
    return normalized


def _timestamp(value: datetime) -> str:
    resolved = value if value.tzinfo else value.replace(tzinfo=UTC)
    return resolved.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _encode_history_id(source_id: str, native_id: str) -> str:
    raw = json.dumps(
        {"source": source_id, "id": native_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "wgh_" + urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_history_id(value: str) -> tuple[str, str]:
    if not value.startswith("wgh_") or len(value) > 2048:
        raise GovernanceHistoryNotFound("governance-history record was not found")
    try:
        raw = value[4:]
        payload = json.loads(urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, json.JSONDecodeError) as exc:
        raise GovernanceHistoryNotFound(
            "governance-history record was not found",
        ) from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"source", "id"}
        or not _string_value(payload.get("source"))
        or not _string_value(payload.get("id"))
    ):
        raise GovernanceHistoryNotFound("governance-history record was not found")
    return payload["source"], payload["id"]


def _encode_cursor(record: dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "v": 1,
            "occurred_at": record["occurred_at"],
            "history_id": record["history_id"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[str, str]:
    if len(value) > 2048:
        raise GovernanceHistoryRequestError("cursor is malformed")
    try:
        payload = json.loads(urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "occurred_at", "history_id"}
            or payload["v"] != 1
            or not isinstance(payload["occurred_at"], str)
            or not isinstance(payload["history_id"], str)
        ):
            raise ValueError
        datetime.fromisoformat(payload["occurred_at"].replace("Z", "+00:00"))
        _decode_history_id(payload["history_id"])
    except (ValueError, json.JSONDecodeError, GovernanceHistoryNotFound) as exc:
        raise GovernanceHistoryRequestError("cursor is malformed") from exc
    return payload["occurred_at"], payload["history_id"]


def _sort_key(record: dict[str, Any]) -> tuple[str, str]:
    return record["occurred_at"], record["history_id"]


def _matches(
    record: dict[str, Any],
    *,
    outcome: str | None,
    subject: str | None,
) -> bool:
    if outcome and record["outcome"].lower() != outcome:
        return False
    return not subject or subject in record["subject"].lower()


def _optional_filter(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized or len(normalized) > 128:
        raise GovernanceHistoryRequestError(f"{label} filter is invalid")
    return normalized
