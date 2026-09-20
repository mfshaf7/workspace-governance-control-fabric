"""Pure Prototype Closure readiness checks; never mutate source or target."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Protocol

from .prototype_closure_authority import ClosureSource, PrototypeClosureUnavailable
from .prototype_maturity_contracts import digest


@dataclass(frozen=True)
class VerifiedReference:
    """A resolver's owner-backed readback, not a caller-submitted claim."""

    ref: str
    owner_ref: str
    digest: str
    state: str
    subject_ref: str | None = None
    source_revision: str | None = None
    source_packet_ref: str | None = None
    prototype_id: str | None = None


class ClosureEvidenceResolver(Protocol):
    def resolve(
        self, request: Mapping[str, Any], source: ClosureSource
    ) -> Mapping[str, VerifiedReference]: ...


ACTION_EVIDENCE = {
    "apply-delivery": {
        "accepted_baseline_receipt_ref": "operator-orchestration-service",
        "accepted_delivery_target_receipt_ref": "operator-orchestration-service",
        "target_delivery_ref": "workspace-delivery-art",
    },
    "graduate-source": {
        "accepted_delivery_target_receipt_ref": "operator-orchestration-service",
        "durable_owner_acceptance_ref": "requested-owner",
        "source_transfer_receipt_ref": "requested-owner",
    },
    "retire-incubation": {
        "retention_plan_ref": "workspace-prototype-studio",
        "runtime_disposition_plan_ref": "platform-engineering",
        "runtime_disposition_proof_ref": "platform-engineering",
    },
    "reopen-incubation": {
        "prior_retirement_receipt_ref": "operator-orchestration-service",
        "retained_source_readback_ref": "workspace-prototype-studio",
    },
}
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_REF = re.compile(r"^[a-z][a-z0-9+.-]*://[A-Za-z0-9][A-Za-z0-9._~:/%+=-]*$")
SAFE_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
DELIVERY_TARGET = re.compile(r"^openproject://work_packages/[1-9][0-9]*$")


def evaluate_prototype_closure(
    request: dict[str, Any],
    source: ClosureSource,
    evidence: Mapping[str, VerifiedReference],
    policy: Mapping[str, Any],
    expected_record_digest: str,
) -> dict[str, Any]:
    action = request["action"]
    checks: list[dict[str, Any]] = []
    findings: list[dict[str, str]] = []

    def check(check_id: str, state: str, code: str, detail: str, owner_ref: str) -> None:
        checks.append({"id": check_id, "state": state})
        if state != "ready":
            findings.append({"code": code, "detail": detail, "owner_ref": owner_ref})

    expected_from = policy["actions"][action]["from_lifecycle"]
    allowed_from = {expected_from} if isinstance(expected_from, str) else set(expected_from)
    if request["expected_source_revision"] != source.revision:
        check("source-revision", "stale", "source-revision-stale",
              "Prototype Studio has a newer committed source revision.", "workspace-prototype-studio")
    else:
        check("source-revision", "ready", "", "", "")
    if expected_record_digest != source.record_digest:
        check("source-record", "stale", "source-record-stale",
              "Prototype record changed since the request was prepared.", "workspace-prototype-studio")
    else:
        check("source-record", "ready", "", "", "")
    if request["expected_lifecycle"] != source.lifecycle or source.lifecycle not in allowed_from:
        check("lifecycle", "blocked", "invalid-lifecycle",
              f"{action} cannot start from the committed {source.lifecycle} lifecycle.",
              "workspace-prototype-studio")
    else:
        check("lifecycle", "ready", "", "", "")

    custody_valid = source.custody == "incubation-repo"
    if action == "retire-incubation":
        custody_valid = source.custody in {
            "incubation-repo", "dedicated-owner-repo", "shared-owner-repo"
        }
    if not custody_valid:
        check("source-custody", "blocked", "custody-conflict",
              "Source custody does not allow this incubation transition.",
              "workspace-prototype-studio")
    else:
        check("source-custody", "ready", "", "", "")

    source_valid = True
    if action == "apply-delivery":
        source_valid = bool(
            source.record.get("delivery_packet_ref")
            and source.record.get("design_baseline_ref")
            and request.get("target_kind") == "new-delivery-epic"
            and DELIVERY_TARGET.fullmatch(request.get("target_delivery_ref", ""))
            and request.get("accepted_delivery_target_receipt_ref")
        )
    elif action == "graduate-source":
        source_valid = (
            source.record.get("project_phase") == "delivery-governed"
            and bool(source.record.get("delivery_packet_ref"))
            and source.record.get("accepted_delivery_target_receipt_ref")
            == request["accepted_delivery_target_receipt_ref"]
        )
    elif action == "reopen-incubation":
        source_valid = (
            source.last_event is not None
            and source.last_event.get("event_type") == "incubation-retired"
            and source.record.get("retirement_ref")
            == source.record.get("closure_event_ref")
        )
    check("source-preconditions", "ready" if source_valid else "blocked",
          "source-precondition-missing", "Committed Studio source lacks the action-specific prerequisite.",
          "workspace-prototype-studio")

    needed = dict(ACTION_EVIDENCE[action])
    if action == "graduate-source":
        if request["transfer_strategy"] == "already-owned":
            needed.pop("source_transfer_receipt_ref")
            needed["already_owned_source_proof_ref"] = "requested-owner"
    for field, owner in needed.items():
        proof = evidence.get(field)
        requested = request.get(field)
        expected_owner = request.get("durable_owner_ref") if owner == "requested-owner" else owner
        if proof is None or proof.state != "accepted":
            check(field, "blocked", "evidence-unavailable",
                  f"Accepted {field} readback is missing or not active.", expected_owner)
            continue
        if proof.owner_ref != expected_owner or not SHA256.fullmatch(proof.digest):
            check(field, "blocked", "evidence-authority-mismatch",
                  f"{field} is not bound to the required owner and digest.", expected_owner)
            continue
        if requested is not None and proof.ref != requested:
            check(field, "blocked", "evidence-reference-mismatch",
                  f"{field} readback differs from the requested reference.", expected_owner)
            continue
        if field == "accepted_delivery_target_receipt_ref" and action == "graduate-source":
            if proof.ref != source.record.get("accepted_delivery_target_receipt_ref"):
                check(field, "blocked", "delivery-target-mismatch",
                      "Accepted Delivery target differs from committed Studio truth.", expected_owner)
                continue
        if field == "accepted_delivery_target_receipt_ref":
            if (proof.source_packet_ref != source.record.get("delivery_packet_ref")
                    or proof.prototype_id != request["prototype_id"]):
                check(field, "blocked", "delivery-source-mismatch",
                      "Delivery acceptance does not bind this Prototype packet.", expected_owner)
                continue
        if field == "accepted_delivery_target_receipt_ref" and action == "apply-delivery":
            target = evidence.get("target_delivery_ref")
            if target is None or proof.subject_ref != target.ref:
                check(field, "blocked", "delivery-target-mismatch",
                      "Delivery acceptance does not bind the selected target.", expected_owner)
                continue
        if field == "accepted_baseline_receipt_ref":
            if proof.subject_ref != source.record.get("design_baseline_ref"):
                check(field, "blocked", "baseline-receipt-mismatch",
                      "Accepted baseline receipt does not bind the staged design baseline.", expected_owner)
                continue
            if proof.prototype_id != request["prototype_id"]:
                check(field, "blocked", "baseline-source-mismatch",
                      "Accepted baseline receipt does not bind this Prototype.", expected_owner)
                continue
        if field in {"source_transfer_receipt_ref", "already_owned_source_proof_ref", "durable_owner_acceptance_ref"}:
            if proof.subject_ref != request.get("durable_repo_ref"):
                check(field, "blocked", "durable-repo-mismatch",
                      f"{field} does not identify the selected durable repository.", expected_owner)
                continue
        if field in {"source_transfer_receipt_ref", "already_owned_source_proof_ref"}:
            if proof.source_revision != source.revision:
                check(field, "blocked", "source-transfer-stale",
                      "Durable source proof does not bind this exact Studio revision.", expected_owner)
                continue
        if field == "target_delivery_ref" and request.get("target_delivery_ref"):
            if proof.ref != request["target_delivery_ref"]:
                check(field, "blocked", "delivery-target-mismatch",
                      "Accepted target differs from the requested Delivery item.", expected_owner)
                continue
        if field == "prior_retirement_receipt_ref" and source.last_event:
            if proof.subject_ref != source.record.get("retirement_ref"):
                check(field, "blocked", "retirement-receipt-mismatch",
                      "Retirement receipt does not bind the active Studio retirement event.", expected_owner)
                continue
        if field == "runtime_disposition_proof_ref":
            if proof.subject_ref != request.get("runtime_disposition_plan_ref"):
                check(field, "blocked", "runtime-plan-mismatch",
                      "Runtime disposition proof does not bind the accepted plan.", expected_owner)
                continue
        if field == "retained_source_readback_ref" and proof.source_revision != source.revision:
            check(field, "blocked", "retained-source-stale",
                  "Retained source readback does not bind current Studio source.", expected_owner)
            continue
        check(field, "ready", "", "", "")

    if action == "retire-incubation" and not request["retirement_reason"].strip():
        check("operator-decision", "blocked", "retirement-decision-missing",
              "Retirement requires an explicit operator decision.", "operator-orchestration-service")
    else:
        check("operator-decision", "ready", "", "", "")
    outcome = "blocked" if any(row["state"] == "blocked" for row in checks) else (
        "stale" if any(row["state"] == "stale" for row in checks) else "ready"
    )
    evidence_rows = [
        {
            "field": field,
            "ref": proof.ref,
            "owner_ref": proof.owner_ref,
            "digest": proof.digest,
            "state": proof.state,
            "subject_ref": proof.subject_ref,
            "source_revision": proof.source_revision,
            "source_packet_ref": proof.source_packet_ref,
            "prototype_id": proof.prototype_id,
        }
        for field, proof in sorted(evidence.items())
    ]
    return {
        "outcome": outcome,
        "checks": checks,
        "findings": findings,
        "evidence": evidence_rows,
        "evidence_digest": digest(evidence_rows),
    }


def resolve_evidence(
    resolver: ClosureEvidenceResolver,
    request: dict[str, Any],
    source: ClosureSource,
) -> Mapping[str, VerifiedReference]:
    try:
        evidence = resolver.resolve(request, source)
    except Exception as exc:
        raise PrototypeClosureUnavailable("Closure evidence authority is unavailable") from exc
    if not isinstance(evidence, Mapping) or len(evidence) > 16 or any(
        not isinstance(key, str) or not isinstance(value, VerifiedReference)
        for key, value in evidence.items()
    ):
        raise PrototypeClosureUnavailable("Closure evidence resolver returned invalid proof")
    allowed = set(ACTION_EVIDENCE[request["action"]])
    if request["action"] == "graduate-source" and request["transfer_strategy"] == "already-owned":
        allowed.remove("source_transfer_receipt_ref")
        allowed.add("already_owned_source_proof_ref")
    if set(evidence) - allowed:
        raise PrototypeClosureUnavailable("Closure evidence resolver returned unrelated proof")
    for proof in evidence.values():
        if (
            len(proof.ref) > 512 or not SAFE_REF.fullmatch(proof.ref)
            or len(proof.owner_ref) > 256 or not SAFE_OWNER.fullmatch(proof.owner_ref)
            or not SHA256.fullmatch(proof.digest)
            or proof.state not in {"accepted", "missing", "revoked", "stale", "denied"}
            or (proof.subject_ref is not None and (
                len(proof.subject_ref) > 512 or not SAFE_REF.fullmatch(proof.subject_ref)
            ))
            or (proof.source_revision is not None and not re.fullmatch(r"[0-9a-f]{40}", proof.source_revision))
            or (proof.source_packet_ref is not None and (
                len(proof.source_packet_ref) > 512 or not SAFE_REF.fullmatch(proof.source_packet_ref)
            ))
            or (proof.prototype_id is not None and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", proof.prototype_id))
        ):
            raise PrototypeClosureUnavailable("Closure evidence resolver returned unsafe proof metadata")
    return evidence
