"""Policy admission decision helpers for the control-fabric foundation.

This module implements bootstrap runtime mechanics for policy/admission
decisions. It consumes authority refs and receipt refs supplied by upstream
systems; it does not define workspace policy truth.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from .validation_execution import LedgerEvent


POLICY_DECISION_SCHEMA_VERSION = 1
LEDGER_SCHEMA_VERSION = 1
SUPPORTED_SUBJECT_TYPES = {"component", "repo"}
SUCCESSFUL_RECEIPT_OUTCOMES = {"success", "passed", "valid", "allow", "allowed"}
FAILED_RECEIPT_OUTCOMES = {"blocked", "denied", "deny", "failure", "failed"}


@dataclass(frozen=True)
class PolicyReason:
    """One machine-readable reason for a policy decision."""

    code: str
    detail: str
    severity: str

    def to_record(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class PolicyDecision:
    """Compact policy/admission decision record."""

    authority_refs: tuple[dict[str, Any], ...]
    decision_id: str
    decision_time: str
    outcome: str
    profile_id: str
    reasons: tuple[PolicyReason, ...]
    receipt_refs: tuple[dict[str, str], ...]
    required_actions: tuple[str, ...]
    schema_version: int
    subject_id: str
    subject_type: str
    target: str
    waiver: dict[str, Any] | None

    def to_record(self) -> dict[str, Any]:
        return {
            "authority_refs": list(self.authority_refs),
            "decision_id": self.decision_id,
            "decision_time": self.decision_time,
            "outcome": self.outcome,
            "profile_id": self.profile_id,
            "reasons": [reason.to_record() for reason in self.reasons],
            "receipt_refs": list(self.receipt_refs),
            "required_actions": list(self.required_actions),
            "schema_version": self.schema_version,
            "subject_id": self.subject_id,
            "subject_type": self.subject_type,
            "target": self.target,
            "waiver": self.waiver,
        }


def evaluate_admission_policy(
    subject: dict[str, Any],
    *,
    profile_id: str = "local-read-only",
    now: datetime | str | None = None,
) -> PolicyDecision:
    """Evaluate bootstrap admission posture for a repo or component."""

    decision_time = _coerce_timestamp(now)
    decision_time_text = decision_time.isoformat().replace("+00:00", "Z")
    subject_type = _optional_str(subject.get("subject_type") or subject.get("type"))
    subject_id = _optional_str(subject.get("subject_id") or subject.get("id"))
    target = _target(subject_type, subject_id)
    authority_refs = tuple(_authority_refs(subject.get("authority_refs")))
    receipt_refs = tuple(_receipt_refs(subject.get("receipt_refs")))
    waiver = _valid_waiver(subject.get("waiver"), decision_time)
    reasons: list[PolicyReason] = []
    required_actions: list[str] = []

    if not subject_type or subject_type not in SUPPORTED_SUBJECT_TYPES:
        reasons.append(
            PolicyReason(
                code="unsupported-subject-type",
                detail="policy admission currently supports repo and component subjects",
                severity="deny",
            ),
        )
        required_actions.append("route subject through an approved intake contract")

    if not subject_id:
        reasons.append(
            PolicyReason(
                code="missing-subject-id",
                detail="subject_id is required for admission decisions",
                severity="deny",
            ),
        )
        required_actions.append("provide a stable subject id")

    if not _optional_str(subject.get("owner_repo")):
        reasons.append(
            PolicyReason(
                code="missing-owner-repo",
                detail="owner_repo is required before admission",
                severity="deny",
            ),
        )
        required_actions.append("declare an owner repo in upstream authority")

    if not authority_refs:
        reasons.append(
            PolicyReason(
                code="missing-authority-ref",
                detail="at least one upstream authority ref is required",
                severity="review",
            ),
        )
        required_actions.append("link the subject to workspace-governance authority refs")
    else:
        for authority_ref in authority_refs:
            freshness = _optional_str(authority_ref.get("freshness_status")) or "unknown"
            if freshness.lower() != "current":
                reasons.append(
                    PolicyReason(
                        code="stale-authority-ref",
                        detail=f"authority ref {authority_ref['authority_id']} is {freshness}",
                        severity="block",
                    ),
                )
                required_actions.append("refresh authority refs before admission")

    validation_required = bool(subject.get("validation_required", True))
    if validation_required:
        validation_reason = _validation_reason(receipt_refs)
        if validation_reason is not None:
            if waiver is not None:
                reasons.append(
                    PolicyReason(
                        code="validation-waived",
                        detail=f"validation issue {validation_reason.code} is covered by waiver {waiver['waiver_id']}",
                        severity="waive",
                    ),
                )
            else:
                reasons.append(validation_reason)
                required_actions.append("provide a successful receipt or explicit approved waiver")

    outcome = _outcome(reasons)
    decision_payload = {
        "authority_refs": authority_refs,
        "decision_time": decision_time_text,
        "outcome": outcome,
        "profile_id": profile_id,
        "reasons": [reason.to_record() for reason in reasons],
        "receipt_refs": receipt_refs,
        "required_actions": sorted(set(required_actions)),
        "subject_id": subject_id or "unknown",
        "subject_type": subject_type or "unknown",
        "target": target,
        "waiver": waiver,
    }
    decision_digest = _digest_json(decision_payload).removeprefix("sha256:")
    return PolicyDecision(
        authority_refs=authority_refs,
        decision_id=f"policy-decision:{decision_digest[:24]}",
        decision_time=decision_time_text,
        outcome=outcome,
        profile_id=profile_id,
        reasons=tuple(reasons),
        receipt_refs=receipt_refs,
        required_actions=tuple(sorted(set(required_actions))),
        schema_version=POLICY_DECISION_SCHEMA_VERSION,
        subject_id=subject_id or "unknown",
        subject_type=subject_type or "unknown",
        target=target,
        waiver=waiver,
    )


def build_policy_ledger_event(*, actor: str, decision: PolicyDecision) -> LedgerEvent:
    """Build a receipt-linked ledger event for a policy decision."""

    receipt_refs = tuple(decision.receipt_refs)
    digest_payload = {
        "action": "policy.decision.recorded",
        "actor": actor,
        "decision_id": decision.decision_id,
        "event_time": decision.decision_time,
        "outcome": decision.outcome,
        "receipt_refs": receipt_refs,
        "target": decision.target,
    }
    event_digest = _digest_json(digest_payload).removeprefix("sha256:")
    return LedgerEvent(
        action="policy.decision.recorded",
        actor=actor,
        artifact_refs=(),
        event_id=f"ledger-event:{event_digest[:24]}",
        event_time=decision.decision_time,
        outcome=decision.outcome,
        receipt_refs=receipt_refs,
        schema_version=LEDGER_SCHEMA_VERSION,
        target=decision.target,
    )


def _validation_reason(receipt_refs: tuple[dict[str, str], ...]) -> PolicyReason | None:
    if not receipt_refs:
        return PolicyReason(
            code="missing-validation-receipt",
            detail="validation is required but no receipt ref was supplied",
            severity="review",
        )

    for receipt_ref in receipt_refs:
        outcome = _optional_str(receipt_ref.get("outcome") or receipt_ref.get("status"))
        if outcome and outcome.lower() in FAILED_RECEIPT_OUTCOMES:
            return PolicyReason(
                code="validation-receipt-not-successful",
                detail=f"receipt {receipt_ref['receipt_id']} outcome is {outcome}",
                severity="block",
            )
        if outcome and outcome.lower() not in SUCCESSFUL_RECEIPT_OUTCOMES:
            return PolicyReason(
                code="validation-receipt-unknown-outcome",
                detail=f"receipt {receipt_ref['receipt_id']} outcome is {outcome}",
                severity="review",
            )
    return None


def _outcome(reasons: list[PolicyReason]) -> str:
    severities = {reason.severity for reason in reasons}
    if "deny" in severities:
        return "deny"
    if "block" in severities:
        return "blocked"
    if "review" in severities:
        return "review_required"
    if "waive" in severities:
        return "waived"
    return "allow"


def _valid_waiver(value: Any, now: datetime) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    waiver_id = _optional_str(value.get("waiver_id"))
    authority_ref_id = _optional_str(value.get("authority_ref_id"))
    reason = _optional_str(value.get("reason"))
    expires_at = _parse_timestamp(value.get("expires_at"))
    if not waiver_id or not authority_ref_id or not reason or expires_at is None:
        return None
    if expires_at < now:
        return None
    return {
        "authority_ref_id": authority_ref_id,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "reason": reason,
        "waiver_id": waiver_id,
    }


def _authority_refs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    refs: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        authority_id = _optional_str(item.get("authority_id"))
        if not authority_id:
            continue
        refs.append(
            {
                "authority_id": authority_id,
                "digest": _optional_str(item.get("digest")),
                "freshness_status": _optional_str(item.get("freshness_status")) or "unknown",
            },
        )
    return refs


def _receipt_refs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    refs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        receipt_id = _optional_str(item.get("receipt_id"))
        digest = _optional_str(item.get("digest") or item.get("receipt_digest"))
        if not receipt_id or not digest:
            continue
        record = {
            "digest": digest,
            "receipt_id": receipt_id,
        }
        outcome = _optional_str(item.get("outcome") or item.get("status"))
        if outcome:
            record["outcome"] = outcome
        refs.append(record)
    return refs


def _target(subject_type: str | None, subject_id: str | None) -> str:
    if subject_type and subject_id:
        return f"{subject_type}:{subject_id}"
    return "unknown"


def _coerce_timestamp(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise ValueError(f"invalid policy decision timestamp {value!r}")
    return parsed


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _digest_json(value: dict[str, Any]) -> str:
    return "sha256:" + sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()
