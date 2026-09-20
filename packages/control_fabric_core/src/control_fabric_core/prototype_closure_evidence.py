"""Owner-routed evidence lookups for Prototype Closure readiness."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from .prototype_closure_authority import ClosureSource, PrototypeClosureUnavailable
from .prototype_closure_policy import ACTION_EVIDENCE, VerifiedReference


@dataclass(frozen=True)
class ClosureEvidenceLookup:
    field: str
    owner_ref: str
    prototype_id: str
    requested_ref: str | None
    subject_ref: str | None
    source_revision: str
    source_packet_ref: str | None
    target_delivery_ref: str | None
    accepted_delivery_target_receipt_ref: str | None
    operator_id: str
    retirement_reason: str | None


class ClosureOwnerReader(Protocol):
    """Read one proof from its owner; never accept a caller-supplied proof body."""

    def read(self, lookup: ClosureEvidenceLookup) -> VerifiedReference | None: ...


class OwnerBackedClosureEvidenceResolver:
    def __init__(self, readers: Mapping[str, ClosureOwnerReader]) -> None:
        self.readers = MappingProxyType(dict(readers))

    def resolve(
        self, request: Mapping[str, Any], source: ClosureSource
    ) -> Mapping[str, VerifiedReference]:
        action = request["action"]
        needed = dict(ACTION_EVIDENCE[action])
        if action == "graduate-source" and request["transfer_strategy"] == "already-owned":
            needed.pop("source_transfer_receipt_ref")
            needed["already_owned_source_proof_ref"] = "requested-owner"

        evidence: dict[str, VerifiedReference] = {}
        for field, owner in needed.items():
            owner_ref = request["durable_owner_ref"] if owner == "requested-owner" else owner
            reader = self.readers.get(owner_ref)
            if reader is None and owner == "requested-owner":
                reader = self.readers.get("requested-owner")
            if reader is None:
                raise PrototypeClosureUnavailable(f"Closure owner reader is unavailable: {owner_ref}")
            lookup = ClosureEvidenceLookup(
                field=field,
                owner_ref=owner_ref,
                prototype_id=request["prototype_id"],
                requested_ref=request.get(field),
                subject_ref=self._subject(field, request, source),
                source_revision=source.revision,
                source_packet_ref=(
                    source.record.get("delivery_packet_ref")
                    if field in {"accepted_delivery_target_receipt_ref", "target_delivery_ref"} else None
                ),
                target_delivery_ref=request.get("target_delivery_ref"),
                accepted_delivery_target_receipt_ref=(
                    request.get("accepted_delivery_target_receipt_ref")
                    or source.record.get("accepted_delivery_target_receipt_ref")
                ),
                operator_id=request["operator_id"],
                retirement_reason=request.get("retirement_reason"),
            )
            proof = reader.read(lookup)
            if proof is not None:
                evidence[field] = proof
        return evidence

    @staticmethod
    def _subject(
        field: str, request: Mapping[str, Any], source: ClosureSource
    ) -> str | None:
        if field == "accepted_baseline_receipt_ref":
            return source.record.get("design_baseline_ref")
        if field in {"accepted_delivery_target_receipt_ref", "target_delivery_ref"}:
            return request.get("target_delivery_ref") if request["action"] == "apply-delivery" else None
        if field in {
            "durable_owner_acceptance_ref", "source_transfer_receipt_ref",
            "already_owned_source_proof_ref",
        }:
            return request.get("durable_repo_ref")
        if field == "runtime_disposition_proof_ref":
            return request.get("runtime_disposition_plan_ref")
        if field == "prior_retirement_receipt_ref":
            return source.record.get("retirement_ref")
        return None
