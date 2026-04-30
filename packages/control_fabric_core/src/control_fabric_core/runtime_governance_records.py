"""Runtime governance records for operator decisions and evidence links.

These records make blocker, approval, waiver, risk, and change evidence visible
to the control fabric without moving authority out of the owning systems.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Iterable

from .validation_execution import LedgerEvent


RUNTIME_GOVERNANCE_RECORD_SCHEMA_VERSION = 1
LEDGER_SCHEMA_VERSION = 1
AUTHORITY_BOUNDARY = "record-only-not-authority"

BLOCKER_DECISION_PATHS = {"accept-risk", "defer", "remove", "workaround"}
APPROVAL_OUTCOMES = {"approved", "rejected", "review_required"}
WAIVER_OUTCOMES = {"expired", "waived"}
RISK_POSTURES = {"accepted", "mitigated", "owned", "resolved"}

LEDGER_ACTION_BY_RECORD_TYPE = {
    "approval_decision": "governance.approval.recorded",
    "blocker_decision": "governance.blocker.recorded",
    "change_record": "governance.change.recorded",
    "risk_posture": "governance.risk.recorded",
    "waiver_decision": "governance.waiver.recorded",
}


@dataclass(frozen=True)
class RuntimeGovernanceRecord:
    """Compact fabric-local governance record."""

    authority_boundary: str
    authority_refs: tuple[dict[str, str], ...]
    created_at: str
    decision: str
    details: dict[str, Any]
    evidence_refs: tuple[dict[str, str], ...]
    next_required_action: str | None
    owner_repo: str
    record_id: str
    record_type: str
    schema_version: int
    target: str

    def to_record(self) -> dict[str, Any]:
        return {
            "authority_boundary": self.authority_boundary,
            "authority_refs": list(self.authority_refs),
            "created_at": self.created_at,
            "decision": self.decision,
            "details": self.details,
            "evidence_refs": list(self.evidence_refs),
            "next_required_action": self.next_required_action,
            "owner_repo": self.owner_repo,
            "record_id": self.record_id,
            "record_type": self.record_type,
            "schema_version": self.schema_version,
            "target": self.target,
        }


def record_blocker_decision(
    *,
    blocker_owner: str,
    decision_path: str,
    impact: str,
    next_required_action: str,
    owner_repo: str,
    statement: str,
    target: str,
    authority_refs: Iterable[dict[str, str]] = (),
    evidence_refs: Iterable[dict[str, str]] = (),
    now: datetime | str | None = None,
) -> RuntimeGovernanceRecord:
    """Record a blocker or impediment decision without mutating ART state."""

    decision = _required_value(decision_path, "decision_path")
    if decision not in BLOCKER_DECISION_PATHS:
        raise ValueError(
            "blocker decision_path must be one of: "
            + ", ".join(sorted(BLOCKER_DECISION_PATHS)),
        )
    details = {
        "blocker_owner": _required_value(blocker_owner, "blocker_owner"),
        "impact": _required_value(impact, "impact"),
        "statement": _required_value(statement, "statement"),
    }
    return _record(
        "blocker_decision",
        decision=decision,
        owner_repo=owner_repo,
        target=target,
        authority_refs=authority_refs,
        evidence_refs=evidence_refs,
        details=details,
        next_required_action=next_required_action,
        now=now,
    )


def record_approval_decision(
    *,
    approval_ref: str,
    approver: str,
    authority_refs: Iterable[dict[str, str]],
    decision: str,
    owner_repo: str,
    target: str,
    evidence_refs: Iterable[dict[str, str]] = (),
    next_required_action: str | None = None,
    now: datetime | str | None = None,
) -> RuntimeGovernanceRecord:
    """Record an approval reference while preserving upstream authority."""

    normalized_decision = _required_value(decision, "decision")
    if normalized_decision not in APPROVAL_OUTCOMES:
        raise ValueError(
            "approval decision must be one of: " + ", ".join(sorted(APPROVAL_OUTCOMES)),
        )
    details = {
        "approval_ref": _required_value(approval_ref, "approval_ref"),
        "approver": _required_value(approver, "approver"),
    }
    return _record(
        "approval_decision",
        decision=normalized_decision,
        owner_repo=owner_repo,
        target=target,
        authority_refs=authority_refs,
        evidence_refs=evidence_refs,
        details=details,
        next_required_action=next_required_action,
        now=now,
        require_authority_refs=True,
    )


def record_waiver_decision(
    *,
    authority_refs: Iterable[dict[str, str]],
    expires_at: str,
    owner_repo: str,
    reason: str,
    target: str,
    waiver_id: str,
    evidence_refs: Iterable[dict[str, str]] = (),
    now: datetime | str | None = None,
) -> RuntimeGovernanceRecord:
    """Record a waiver reference without granting authority locally."""

    decision = "expired" if _coerce_timestamp(expires_at) <= _coerce_timestamp(now) else "waived"
    details = {
        "expires_at": _required_value(expires_at, "expires_at"),
        "reason": _required_value(reason, "reason"),
        "waiver_id": _required_value(waiver_id, "waiver_id"),
    }
    next_required_action = None
    if decision == "expired":
        next_required_action = "refresh waiver or remove the exception"
    return _record(
        "waiver_decision",
        decision=decision,
        owner_repo=owner_repo,
        target=target,
        authority_refs=authority_refs,
        evidence_refs=evidence_refs,
        details=details,
        next_required_action=next_required_action,
        now=now,
        require_authority_refs=True,
    )


def record_risk_posture(
    *,
    owner_repo: str,
    risk_owner: str,
    risk_ref: str,
    roam_state: str,
    target: str,
    authority_refs: Iterable[dict[str, str]] = (),
    evidence_refs: Iterable[dict[str, str]] = (),
    next_required_action: str | None = None,
    now: datetime | str | None = None,
) -> RuntimeGovernanceRecord:
    """Record runtime risk posture without owning risk acceptance."""

    decision = _required_value(roam_state, "roam_state")
    if decision not in RISK_POSTURES:
        raise ValueError("roam_state must be one of: " + ", ".join(sorted(RISK_POSTURES)))
    details = {
        "risk_owner": _required_value(risk_owner, "risk_owner"),
        "risk_ref": _required_value(risk_ref, "risk_ref"),
    }
    return _record(
        "risk_posture",
        decision=decision,
        owner_repo=owner_repo,
        target=target,
        authority_refs=authority_refs,
        evidence_refs=evidence_refs,
        details=details,
        next_required_action=next_required_action,
        now=now,
    )


def record_change_event(
    *,
    changed_surfaces: Iterable[str],
    evidence_refs: Iterable[dict[str, str]],
    owner_repo: str,
    record_ref: str,
    target: str,
    authority_refs: Iterable[dict[str, str]] = (),
    now: datetime | str | None = None,
) -> RuntimeGovernanceRecord:
    """Record a change event that links to compact evidence refs."""

    surfaces = tuple(_clean_values(changed_surfaces))
    if not surfaces:
        raise ValueError("changed_surfaces must include at least one surface")
    details = {
        "changed_surfaces": list(surfaces),
        "record_ref": _required_value(record_ref, "record_ref"),
    }
    return _record(
        "change_record",
        decision="recorded",
        owner_repo=owner_repo,
        target=target,
        authority_refs=authority_refs,
        evidence_refs=evidence_refs,
        details=details,
        next_required_action=None,
        now=now,
        require_evidence_refs=True,
    )


def build_governance_record_ledger_event(
    *,
    actor: str,
    record: RuntimeGovernanceRecord,
) -> LedgerEvent:
    """Build a ledger event for a runtime governance record."""

    action = LEDGER_ACTION_BY_RECORD_TYPE[record.record_type]
    receipt_refs = tuple(
        {
            key: value
            for key, value in evidence_ref.items()
            if key in {"digest", "outcome", "receipt_id"}
        }
        for evidence_ref in record.evidence_refs
        if evidence_ref.get("receipt_id") and evidence_ref.get("digest")
    )
    digest_payload = {
        "action": action,
        "actor": actor,
        "event_time": record.created_at,
        "outcome": record.decision,
        "receipt_refs": receipt_refs,
        "record_id": record.record_id,
        "target": record.target,
    }
    event_digest = _digest_json(digest_payload).removeprefix("sha256:")
    return LedgerEvent(
        action=action,
        actor=_required_value(actor, "actor"),
        artifact_refs=(),
        event_id=f"ledger-event:{event_digest[:24]}",
        event_time=record.created_at,
        outcome=record.decision,
        receipt_refs=receipt_refs,
        schema_version=LEDGER_SCHEMA_VERSION,
        target=record.target,
    )


def _record(
    record_type: str,
    *,
    authority_refs: Iterable[dict[str, str]],
    decision: str,
    details: dict[str, Any],
    evidence_refs: Iterable[dict[str, str]],
    next_required_action: str | None,
    owner_repo: str,
    target: str,
    now: datetime | str | None,
    require_authority_refs: bool = False,
    require_evidence_refs: bool = False,
) -> RuntimeGovernanceRecord:
    created_at = _coerce_timestamp(now).isoformat().replace("+00:00", "Z")
    normalized_authority_refs = tuple(_refs(authority_refs, "authority"))
    normalized_evidence_refs = tuple(_refs(evidence_refs, "evidence"))
    if require_authority_refs and not normalized_authority_refs:
        raise ValueError(f"{record_type} requires at least one authority ref")
    if require_evidence_refs and not normalized_evidence_refs:
        raise ValueError(f"{record_type} requires at least one evidence ref")
    record_payload = {
        "authority_boundary": AUTHORITY_BOUNDARY,
        "authority_refs": normalized_authority_refs,
        "created_at": created_at,
        "decision": decision,
        "details": details,
        "evidence_refs": normalized_evidence_refs,
        "next_required_action": next_required_action,
        "owner_repo": _required_value(owner_repo, "owner_repo"),
        "record_type": record_type,
        "target": _required_value(target, "target"),
    }
    record_digest = _digest_json(record_payload).removeprefix("sha256:")
    return RuntimeGovernanceRecord(
        authority_boundary=AUTHORITY_BOUNDARY,
        authority_refs=normalized_authority_refs,
        created_at=created_at,
        decision=decision,
        details=details,
        evidence_refs=normalized_evidence_refs,
        next_required_action=next_required_action,
        owner_repo=str(record_payload["owner_repo"]),
        record_id=f"runtime-governance-record:{record_digest[:24]}",
        record_type=record_type,
        schema_version=RUNTIME_GOVERNANCE_RECORD_SCHEMA_VERSION,
        target=str(record_payload["target"]),
    )


def _refs(values: Iterable[dict[str, str]], ref_kind: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError(f"{ref_kind} refs must be objects")
        ref = {str(key): str(ref_value) for key, ref_value in value.items() if str(ref_value).strip()}
        if not ref:
            raise ValueError(f"{ref_kind} refs must not be empty")
        refs.append(ref)
    return refs


def _required_value(value: str | None, name: str) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        raise ValueError(f"{name} is required")
    return rendered


def _clean_values(values: Iterable[str]) -> list[str]:
    return [str(value).strip() for value in values if str(value).strip()]


def _coerce_timestamp(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _digest_json(value: dict[str, Any]) -> str:
    return "sha256:" + sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()
