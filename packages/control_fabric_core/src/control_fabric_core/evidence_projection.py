"""Compact evidence projections from control receipts.

The control fabric keeps raw runtime artifacts in receipt-linked storage. This
module projects compact, operator-safe receipt summaries into downstream
workflow surfaces without copying raw logs or making those surfaces the
evidence authority.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Iterable

from .policy_admission import PolicyDecision
from .validation_execution import ControlReceipt


EVIDENCE_PROJECTION_SCHEMA_VERSION = 1
SUPPORTED_PROJECTION_TYPES = {
    "art_completion_evidence",
    "change_record_references",
    "review_packet_evidence",
}


@dataclass(frozen=True)
class ReceiptEvidenceRef:
    """Stable compact reference to a control receipt."""

    captured_at: str
    digest: str
    outcome: str
    receipt_id: str
    target_scope: str

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceProjection:
    """Operator-safe evidence projection for one downstream surface."""

    artifact_refs: tuple[dict[str, Any], ...]
    denied_or_suppressed: tuple[str, ...]
    included_evidence: tuple[str, ...]
    policy_decision_refs: tuple[dict[str, str], ...]
    projection_id: str
    projection_time: str
    projection_type: str
    raw_artifacts_embedded: bool
    receipt_ref: ReceiptEvidenceRef
    schema_version: int
    target_surface: str

    def to_record(self) -> dict[str, Any]:
        return {
            "artifact_refs": list(self.artifact_refs),
            "denied_or_suppressed": list(self.denied_or_suppressed),
            "included_evidence": list(self.included_evidence),
            "policy_decision_refs": list(self.policy_decision_refs),
            "projection_id": self.projection_id,
            "projection_time": self.projection_time,
            "projection_type": self.projection_type,
            "raw_artifacts_embedded": self.raw_artifacts_embedded,
            "receipt_ref": self.receipt_ref.to_record(),
            "schema_version": self.schema_version,
            "target_surface": self.target_surface,
        }


@dataclass(frozen=True)
class ArtCompletionEvidenceProjection:
    """Projection payload for Workspace Delivery ART closeout evidence."""

    changed_surfaces: tuple[str, ...]
    completion_summary: str
    projection: EvidenceProjection
    residual_follow_up: tuple[str, ...]
    test_result_evidence: tuple[str, ...]
    validation_evidence: tuple[str, ...]

    def to_record(self) -> dict[str, Any]:
        return {
            "changed_surfaces": list(self.changed_surfaces),
            "completion_summary": self.completion_summary,
            "projection": self.projection.to_record(),
            "residual_follow_up": list(self.residual_follow_up),
            "test_result_evidence": list(self.test_result_evidence),
            "validation_evidence": list(self.validation_evidence),
        }

    def to_completion_payload(self) -> dict[str, str]:
        payload = {
            "changed_surfaces": _markdown_list(self.changed_surfaces),
            "completion_note": _receipt_note(self.projection.receipt_ref),
            "completion_summary": self.completion_summary,
            "test_result_evidence": _markdown_list(self.test_result_evidence),
            "validation_evidence": _markdown_list(self.validation_evidence),
        }
        if self.residual_follow_up:
            payload["residual_follow_up"] = _markdown_list(self.residual_follow_up)
        return payload


@dataclass(frozen=True)
class ReviewPacketEvidenceProjection:
    """Projection payload for source-backed Review Packet evidence."""

    changed_surface_explanations: tuple[str, ...]
    item_evidence_refs: tuple[dict[str, str], ...]
    projection: EvidenceProjection
    rollback_boundary: str
    test_evidence: tuple[str, ...]
    validation_evidence: tuple[str, ...]

    def to_record(self) -> dict[str, Any]:
        return {
            "changed_surface_explanations": list(self.changed_surface_explanations),
            "item_evidence_refs": list(self.item_evidence_refs),
            "projection": self.projection.to_record(),
            "rollback_boundary": self.rollback_boundary,
            "test_evidence": list(self.test_evidence),
            "validation_evidence": list(self.validation_evidence),
        }


@dataclass(frozen=True)
class ChangeRecordReferenceProjection:
    """Projection payload for Git and change-record evidence references."""

    evidence_refs: tuple[dict[str, str], ...]
    projection: EvidenceProjection
    record_note: str

    def to_record(self) -> dict[str, Any]:
        return {
            "evidence_refs": list(self.evidence_refs),
            "projection": self.projection.to_record(),
            "record_note": self.record_note,
        }


def project_receipt_to_art_completion_evidence(
    receipt: ControlReceipt | dict[str, Any],
    *,
    changed_surfaces: Iterable[str],
    completion_summary: str | None = None,
    policy_decisions: Iterable[PolicyDecision | dict[str, Any]] = (),
    residual_follow_up: Iterable[str] = (),
    now: datetime | str | None = None,
) -> ArtCompletionEvidenceProjection:
    """Project a receipt into ART completion-evidence fields."""

    receipt_record = _receipt_record(receipt)
    projection = _base_projection(
        receipt_record,
        "art_completion_evidence",
        "workspace-delivery-art",
        policy_decisions=policy_decisions,
        now=now,
    )
    receipt_ref = projection.receipt_ref
    rendered_summary = completion_summary or (
        f"Validated `{receipt_ref.target_scope}` with receipt `{receipt_ref.receipt_id}` "
        f"and outcome `{receipt_ref.outcome}`."
    )
    return ArtCompletionEvidenceProjection(
        changed_surfaces=tuple(_clean_lines(changed_surfaces)),
        completion_summary=rendered_summary,
        projection=projection,
        residual_follow_up=tuple(_clean_lines(residual_follow_up)),
        test_result_evidence=tuple(_check_result_lines(receipt_record)),
        validation_evidence=tuple(_validation_lines(receipt_record, projection)),
    )


def project_receipt_to_review_packet_evidence(
    receipt: ControlReceipt | dict[str, Any],
    *,
    changed_surface_explanations: Iterable[str],
    item_ids: Iterable[int | str],
    policy_decisions: Iterable[PolicyDecision | dict[str, Any]] = (),
    rollback_boundary: str | None = None,
    now: datetime | str | None = None,
) -> ReviewPacketEvidenceProjection:
    """Project a receipt into source-backed Review Packet evidence."""

    receipt_record = _receipt_record(receipt)
    projection = _base_projection(
        receipt_record,
        "review_packet_evidence",
        "workspace-delivery-art-review-packet",
        policy_decisions=policy_decisions,
        now=now,
    )
    receipt_ref = projection.receipt_ref
    item_evidence_refs = tuple(
        {
            "evidence_type": "control_receipt",
            "item_id": str(item_id),
            "receipt_digest": receipt_ref.digest,
            "receipt_id": receipt_ref.receipt_id,
        }
        for item_id in item_ids
    )
    return ReviewPacketEvidenceProjection(
        changed_surface_explanations=tuple(_clean_lines(changed_surface_explanations)),
        item_evidence_refs=item_evidence_refs,
        projection=projection,
        rollback_boundary=rollback_boundary
        or "Rollback by reverting the source change; runtime evidence remains receipt-linked for audit.",
        test_evidence=tuple(_check_result_lines(receipt_record)),
        validation_evidence=tuple(_validation_lines(receipt_record, projection)),
    )


def project_receipt_to_change_record_references(
    receipt: ControlReceipt | dict[str, Any],
    *,
    change_record_path: str | None = None,
    policy_decisions: Iterable[PolicyDecision | dict[str, Any]] = (),
    now: datetime | str | None = None,
) -> ChangeRecordReferenceProjection:
    """Project receipt and policy refs for Git/change-record surfaces."""

    receipt_record = _receipt_record(receipt)
    projection = _base_projection(
        receipt_record,
        "change_record_references",
        "git-change-record",
        policy_decisions=policy_decisions,
        now=now,
    )
    receipt_ref = projection.receipt_ref
    evidence_refs = [
        {
            "digest": receipt_ref.digest,
            "evidence_type": "control_receipt",
            "outcome": receipt_ref.outcome,
            "receipt_id": receipt_ref.receipt_id,
            "target_scope": receipt_ref.target_scope,
        },
    ]
    for artifact in projection.artifact_refs:
        evidence_refs.append(
            {
                "artifact_id": str(artifact["artifact_id"]),
                "digest": str(artifact["digest"]),
                "evidence_type": "artifact_ref",
                "purpose": str(artifact["purpose"]),
            },
        )
    for decision in projection.policy_decision_refs:
        evidence_refs.append(
            {
                "decision_id": decision["decision_id"],
                "evidence_type": "policy_decision",
                "outcome": decision["outcome"],
            },
        )
    destination = f" for `{change_record_path}`" if change_record_path else ""
    return ChangeRecordReferenceProjection(
        evidence_refs=tuple(evidence_refs),
        projection=projection,
        record_note=(
            f"Use receipt `{receipt_ref.receipt_id}` and digest `{receipt_ref.digest}`"
            f"{destination}; do not copy raw runtime artifacts into Git records."
        ),
    )


def _base_projection(
    receipt_record: dict[str, Any],
    projection_type: str,
    target_surface: str,
    *,
    policy_decisions: Iterable[PolicyDecision | dict[str, Any]],
    now: datetime | str | None,
) -> EvidenceProjection:
    if projection_type not in SUPPORTED_PROJECTION_TYPES:
        raise ValueError(f"unsupported projection_type: {projection_type}")
    projection_time = _coerce_timestamp(now).isoformat().replace("+00:00", "Z")
    receipt_ref = _receipt_ref(receipt_record)
    artifact_refs = tuple(_artifact_refs(receipt_record))
    policy_decision_refs = tuple(_policy_decision_refs(policy_decisions))
    included_evidence = (
        f"receipt {receipt_ref.receipt_id}",
        f"receipt digest {receipt_ref.digest}",
        f"receipt outcome {receipt_ref.outcome}",
        f"{len(artifact_refs)} artifact refs",
        f"{len(receipt_record.get('check_results', []))} check results",
    )
    denied_or_suppressed = (
        "raw validator stdout/stderr omitted from projection",
        "full artifacts remain referenced by digest and path only",
    )
    digest_payload = {
        "artifact_refs": artifact_refs,
        "policy_decision_refs": policy_decision_refs,
        "projection_time": projection_time,
        "projection_type": projection_type,
        "receipt_ref": receipt_ref.to_record(),
        "target_surface": target_surface,
    }
    projection_digest = _digest_json(digest_payload).removeprefix("sha256:")
    return EvidenceProjection(
        artifact_refs=artifact_refs,
        denied_or_suppressed=denied_or_suppressed,
        included_evidence=included_evidence,
        policy_decision_refs=policy_decision_refs,
        projection_id=f"evidence-projection:{projection_digest[:24]}",
        projection_time=projection_time,
        projection_type=projection_type,
        raw_artifacts_embedded=False,
        receipt_ref=receipt_ref,
        schema_version=EVIDENCE_PROJECTION_SCHEMA_VERSION,
        target_surface=target_surface,
    )


def _receipt_record(receipt: ControlReceipt | dict[str, Any]) -> dict[str, Any]:
    record = receipt.to_record() if isinstance(receipt, ControlReceipt) else dict(receipt)
    required = ("captured_at", "digest", "outcome", "receipt_id", "target_scope")
    missing = [key for key in required if not record.get(key)]
    if missing:
        raise ValueError(f"receipt missing required projection fields: {', '.join(missing)}")
    return record


def _receipt_ref(receipt_record: dict[str, Any]) -> ReceiptEvidenceRef:
    return ReceiptEvidenceRef(
        captured_at=str(receipt_record["captured_at"]),
        digest=str(receipt_record["digest"]),
        outcome=str(receipt_record["outcome"]),
        receipt_id=str(receipt_record["receipt_id"]),
        target_scope=str(receipt_record["target_scope"]),
    )


def _artifact_refs(receipt_record: dict[str, Any]) -> list[dict[str, Any]]:
    artifact_refs = receipt_record.get("artifact_refs", [])
    if not isinstance(artifact_refs, list):
        raise ValueError("receipt artifact_refs must be an array")
    return [dict(artifact) for artifact in artifact_refs if isinstance(artifact, dict)]


def _policy_decision_refs(
    policy_decisions: Iterable[PolicyDecision | dict[str, Any]],
) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for decision in policy_decisions:
        record = decision.to_record() if isinstance(decision, PolicyDecision) else dict(decision)
        decision_id = str(record.get("decision_id") or "").strip()
        outcome = str(record.get("outcome") or "").strip()
        if not decision_id or not outcome:
            raise ValueError("policy decision refs require decision_id and outcome")
        refs.append({"decision_id": decision_id, "outcome": outcome})
    return refs


def _validation_lines(
    receipt_record: dict[str, Any],
    projection: EvidenceProjection,
) -> list[str]:
    receipt_ref = projection.receipt_ref
    lines = [
        f"`{receipt_ref.receipt_id}` outcome `{receipt_ref.outcome}` for `{receipt_ref.target_scope}`.",
        f"Receipt digest `{receipt_ref.digest}` captured at `{receipt_ref.captured_at}`.",
        f"Raw artifacts embedded: `{str(projection.raw_artifacts_embedded).lower()}`.",
    ]
    if projection.artifact_refs:
        lines.append(f"Artifact refs retained: `{len(projection.artifact_refs)}`.")
    for decision in projection.policy_decision_refs:
        lines.append(
            f"Policy decision `{decision['decision_id']}` outcome `{decision['outcome']}`."
        )
    return lines


def _check_result_lines(receipt_record: dict[str, Any]) -> list[str]:
    results = receipt_record.get("check_results", [])
    if not results:
        return ["No fresh check results were projected from this receipt."]
    lines: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        status = str(result.get("status") or "unknown")
        validator_id = str(result.get("validator_id") or "unknown-validator")
        check_id = str(result.get("check_id") or "unknown-check")
        exit_code = result.get("exit_code")
        suffix = f", exit code `{exit_code}`" if exit_code is not None else ""
        lines.append(f"`{status.upper()}` `{validator_id}` check `{check_id}`{suffix}.")
    return lines or ["No fresh check results were projected from this receipt."]


def _receipt_note(receipt_ref: ReceiptEvidenceRef) -> str:
    return (
        f"Evidence projected from control receipt `{receipt_ref.receipt_id}` "
        f"with digest `{receipt_ref.digest}`. Raw artifacts remain receipt-linked."
    )


def _markdown_list(values: Iterable[str]) -> str:
    rendered = _clean_lines(values)
    return "\n".join(f"- {value}" for value in rendered)


def _clean_lines(values: Iterable[str]) -> list[str]:
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
