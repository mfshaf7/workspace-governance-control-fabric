"""Artifact-bound Delivery ART readiness evaluation and durable receipts.

OOS authors and semantically validates Delivery ART packets before durable
registry admission. This module revalidates pinned schemas and canonical
integrity, resolves the immutable dependency chain, and owns only the
readiness-specific composition and receipt ledger.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import os
import re
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .artifact_registry import (
    RUNTIME_PROFILE_ENV,
    SERVICE_IDENTITY_ENV,
    DeliveryArtifactRegistry,
    read_implementation_ref,
)
from .canonical_json import canonical_digest, delivery_art_content_projection, strict_json_loads
from .database import create_session_factory
from .db.models import DeliveryArtReadinessReceipt, LedgerEvent
from .delivery_art_contracts import (
    DeliveryArtContractBundle,
    DeliveryArtContractError,
    operating_readiness_subject,
)


MAX_DELIVERY_ART_READINESS_REQUEST_BYTES = 1_081_344
DELIVERY_ART_AUTHORITY_REF = (
    "https://github.com/mfshaf7/workspace-governance/blob/main/"
    "contracts/delivery-art-operator-path.yaml"
)
SECURITY_REVIEW_REF = (
    "https://github.com/mfshaf7/security-architecture/blob/main/"
    "docs/reviews/components/2026-08-09-art-evidence-custody-and-source-provenance.md"
)

_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROFILE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
_RECEIPT_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{24}$")
_SOURCE_URI_PATTERN = re.compile(r"^wgcf://artifacts/delivery-art/sha256/[0-9a-f]{64}$")
_IMPLEMENTATION_REF_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_WORK_ITEM_PATTERN = re.compile(r"^work-item-[1-9][0-9]*$")

_LEVEL_CONTRACTS = {
    "architecture-ready": (
        "delivery_art_architecture_packet",
        "artifact-content",
    ),
    "implementation-ready": (
        "delivery_art_work_start_record",
        "artifact-content",
    ),
    "merge-ready": ("art_review_packet", "artifact-content"),
    "operating-ready": ("art_review_packet", "readiness-subject"),
}
_REQUIRED_INVALIDATION_INPUTS = {
    "art-descendant-or-dependency-change",
    "owner-or-rollback-boundary-change",
    "base-ref-or-commit-change",
    "architecture-decision-or-digest-change",
    "validation-or-security-obligation-change",
}
_EVIDENCE_SECTIONS = (
    "tests",
    "validations",
    "runtime_and_live",
    "security_and_trust",
)


class DeliveryArtReadinessError(RuntimeError):
    """Base failure for artifact-bound Delivery ART readiness."""


class DeliveryArtReadinessContractError(DeliveryArtReadinessError):
    """The readiness request or resolved artifact violates the contract."""


class DeliveryArtReadinessNotFound(DeliveryArtReadinessError):
    """The requested readiness receipt does not exist."""


class DeliveryArtReadinessUnavailable(DeliveryArtReadinessError):
    """The readiness ledger or evaluator is unavailable."""


@dataclass(frozen=True)
class PreparedReadinessRequest:
    profile_id: str
    readiness_request: dict[str, Any]
    subject_ref: dict[str, str] | None
    finalization_candidate: dict[str, Any] | None


@dataclass(frozen=True)
class ReadinessFinding:
    id: str
    severity: str
    summary: str
    authority_ref: str | None = DELIVERY_ART_AUTHORITY_REF

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "summary": self.summary,
            "authority_ref": self.authority_ref,
        }


@dataclass(frozen=True)
class DeliveryArtReadinessResult:
    artifact: dict[str, Any]
    generation: int
    resolution: str

    def to_record(self) -> dict[str, Any]:
        return {
            "artifact": copy.deepcopy(self.artifact),
            "receipt": {
                "generation": self.generation,
                "resolution": self.resolution,
                "state": "durable",
                "ref": {
                    "uri": self.artifact["custody"]["uri"],
                    "digest": self.artifact["integrity"]["content_digest"],
                },
            },
        }


@dataclass(frozen=True)
class _ResolvedReadinessContext:
    subject: dict[str, Any]
    source_artifacts: dict[str, dict[str, Any]]
    source_receipts: dict[str, dict[str, Any]]
    candidate: dict[str, Any] | None


def prepare_delivery_art_readiness_request(raw_request: bytes) -> PreparedReadinessRequest:
    """Parse one bounded readiness request without trusting caller projections."""

    if len(raw_request) > MAX_DELIVERY_ART_READINESS_REQUEST_BYTES:
        raise DeliveryArtReadinessContractError("readiness request exceeds payload limit")
    try:
        request = strict_json_loads(raw_request)
    except (RecursionError, ValueError) as exc:
        raise DeliveryArtReadinessContractError(str(exc)) from exc
    if not isinstance(request, dict):
        raise DeliveryArtReadinessContractError("readiness request must be an object")
    allowed_fields = {
        "schema_version",
        "profile_id",
        "readiness_request",
        "subject_ref",
        "finalization_candidate",
    }
    if not set(request).issubset(allowed_fields):
        raise DeliveryArtReadinessContractError("readiness request contains unsupported fields")
    if request.get("schema_version") != 1:
        raise DeliveryArtReadinessContractError("readiness request schema_version must be 1")
    profile_id = request.get("profile_id")
    if not isinstance(profile_id, str) or not _PROFILE_PATTERN.fullmatch(profile_id):
        raise DeliveryArtReadinessContractError("profile_id is invalid")
    readiness = request.get("readiness_request")
    if not isinstance(readiness, dict) or set(readiness) != {
        "artifact_id",
        "artifact_type",
        "covered_work_item_ids",
        "delivery_id",
        "digest",
        "digest_kind",
        "readiness_level",
    }:
        raise DeliveryArtReadinessContractError("readiness_request fields do not match the contract")
    level = readiness.get("readiness_level")
    contract = _LEVEL_CONTRACTS.get(level)
    if contract is None:
        raise DeliveryArtReadinessContractError("readiness_level is unsupported")
    expected_type, expected_digest_kind = contract
    if readiness.get("artifact_type") != expected_type:
        raise DeliveryArtReadinessContractError("readiness level uses the wrong artifact_type")
    if readiness.get("digest_kind") != expected_digest_kind:
        raise DeliveryArtReadinessContractError("readiness level uses the wrong digest_kind")
    digest = readiness.get("digest")
    if not isinstance(digest, str) or not _DIGEST_PATTERN.fullmatch(digest):
        raise DeliveryArtReadinessContractError("readiness subject digest is invalid")
    delivery_id = readiness.get("delivery_id")
    if not isinstance(delivery_id, str) or not re.fullmatch(r"delivery-[1-9][0-9]*", delivery_id):
        raise DeliveryArtReadinessContractError("delivery_id is invalid")
    artifact_id = readiness.get("artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id.startswith(
        {
            "delivery_art_architecture_packet": f"architecture-packet:{delivery_id}",
            "delivery_art_work_start_record": f"work-start:{delivery_id}",
            "art_review_packet": f"review-packet:{delivery_id}",
        }[expected_type],
    ):
        raise DeliveryArtReadinessContractError("artifact_id is not scoped to delivery_id")
    covered = readiness.get("covered_work_item_ids")
    if (
        not isinstance(covered, list)
        or not covered
        or len(set(covered)) != len(covered)
        or any(not isinstance(item, str) or not _WORK_ITEM_PATTERN.fullmatch(item) for item in covered)
    ):
        raise DeliveryArtReadinessContractError("covered_work_item_ids are invalid")

    subject_ref = request.get("subject_ref")
    candidate = request.get("finalization_candidate")
    if expected_digest_kind == "artifact-content":
        if candidate is not None:
            raise DeliveryArtReadinessContractError(
                "artifact-content readiness must not include a finalization candidate",
            )
        subject_ref = _require_source_ref(subject_ref, "subject_ref")
        if subject_ref["digest"] != digest:
            raise DeliveryArtReadinessContractError("subject_ref digest does not match readiness request")
    else:
        if subject_ref is not None:
            raise DeliveryArtReadinessContractError(
                "operating readiness must not claim a durable final subject",
            )
        if not isinstance(candidate, dict):
            raise DeliveryArtReadinessContractError(
                "operating readiness requires finalization_candidate",
            )
    normalized_readiness = copy.deepcopy(readiness)
    normalized_readiness["covered_work_item_ids"] = sorted(covered)
    return PreparedReadinessRequest(
        profile_id=profile_id,
        readiness_request=normalized_readiness,
        subject_ref=copy.deepcopy(subject_ref),
        finalization_candidate=copy.deepcopy(candidate),
    )


class DeliveryArtReadinessService:
    """Resolve structured ART evidence and issue immutable WGCF decisions."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        artifact_registry: DeliveryArtifactRegistry,
        service_identity_ref: str,
        implementation_ref: str,
        contract_bundle: DeliveryArtContractBundle | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not service_identity_ref.strip():
            raise DeliveryArtReadinessUnavailable("readiness service identity is not configured")
        if not _IMPLEMENTATION_REF_PATTERN.fullmatch(implementation_ref):
            raise DeliveryArtReadinessUnavailable(
                "readiness implementation_ref must be an exact Git commit",
            )
        self._sessions = session_factory
        self._registry = artifact_registry
        self._service_identity_ref = service_identity_ref.strip()
        self._implementation_ref = implementation_ref
        self._contracts = contract_bundle or DeliveryArtContractBundle.load()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(self, raw_request: bytes, *, actor: str) -> DeliveryArtReadinessResult:
        prepared = prepare_delivery_art_readiness_request(raw_request)
        try:
            context = self._resolve_context(prepared, actor=actor)
            findings = self._evaluate(prepared, context)
            return self._persist(prepared, context, findings, actor=actor)
        except DeliveryArtReadinessError:
            raise
        except DeliveryArtContractError as exc:
            raise DeliveryArtReadinessContractError(str(exc)) from exc
        except SQLAlchemyError as exc:
            raise DeliveryArtReadinessUnavailable(
                "readiness receipt ledger is unavailable",
            ) from exc

    def read(self, receipt_token: str, *, actor: str) -> DeliveryArtReadinessResult:
        if not _RECEIPT_TOKEN_PATTERN.fullmatch(receipt_token):
            raise DeliveryArtReadinessContractError("readiness receipt token is invalid")
        receipt_id = f"art-readiness-receipt:{receipt_token}"
        try:
            with self._sessions() as session:
                row = session.get(DeliveryArtReadinessReceipt, receipt_id)
                if row is None:
                    raise DeliveryArtReadinessNotFound("readiness receipt was not found")
                result = self._materialize(row, resolution="read")
            self._append_ledger(actor, "delivery-art.readiness.read", result)
            return result
        except DeliveryArtReadinessError:
            raise
        except SQLAlchemyError as exc:
            raise DeliveryArtReadinessUnavailable(
                "readiness receipt ledger is unavailable",
            ) from exc

    def _resolve_context(
        self,
        prepared: PreparedReadinessRequest,
        *,
        actor: str,
    ) -> _ResolvedReadinessContext:
        artifacts: dict[str, dict[str, Any]] = {}
        receipts: dict[str, dict[str, Any]] = {}

        def resolve(reference: dict[str, str]) -> dict[str, Any]:
            existing = artifacts.get(reference["uri"])
            if existing is not None:
                if existing.get("integrity", {}).get("content_digest") != reference["digest"]:
                    raise DeliveryArtReadinessContractError("dependency reference is ambiguous")
                return existing
            result = self._registry.read(reference["digest"], actor=actor)
            artifact = result.artifact
            custody_receipt = result.custody_receipt
            if (
                artifact.get("custody", {}).get("uri") != reference["uri"]
                or artifact.get("integrity", {}).get("content_digest") != reference["digest"]
            ):
                raise DeliveryArtReadinessContractError(
                    "registry dependency does not match its requested reference",
                )
            self._require_valid_artifact(artifact)
            self._contracts.require_valid(custody_receipt)
            artifacts[reference["uri"]] = artifact
            receipts[custody_receipt["custody"]["uri"]] = custody_receipt
            self._walk_source_dependencies(artifact, resolve)
            return artifact

        if prepared.subject_ref is not None:
            subject = resolve(prepared.subject_ref)
            candidate = None
        else:
            candidate = copy.deepcopy(prepared.finalization_candidate)
            self._require_valid_artifact(candidate)
            subject = operating_readiness_subject(candidate)
            self._walk_source_dependencies(candidate, resolve)
        return _ResolvedReadinessContext(
            subject=subject,
            source_artifacts=artifacts,
            source_receipts=receipts,
            candidate=candidate,
        )

    def _walk_source_dependencies(
        self,
        artifact: dict[str, Any],
        resolve: Callable[[dict[str, str]], dict[str, Any]],
    ) -> None:
        refs: list[dict[str, str]] = []
        supersedes = artifact.get("custody", {}).get("supersedes")
        if supersedes is not None:
            refs.append(_require_source_ref(supersedes, "custody.supersedes"))
        if artifact.get("artifact_type") == "delivery_art_work_start_record":
            architecture = artifact.get("architecture", {})
            if architecture.get("readiness") == "architecture-ready":
                refs.append(
                    _require_source_ref(
                        {
                            "uri": architecture.get("packet_ref"),
                            "digest": architecture.get("packet_digest"),
                        },
                        "architecture packet",
                    ),
                )
        if artifact.get("artifact_type") == "art_review_packet":
            work_start = artifact.get("work_start", {})
            refs.append(
                _require_source_ref(
                    {
                        "uri": work_start.get("artifact_ref"),
                        "digest": work_start.get("artifact_digest"),
                    },
                    "work-start record",
                ),
            )
        for reference in refs:
            resolve(reference)

    def _require_valid_artifact(self, artifact: dict[str, Any]) -> None:
        self._contracts.require_valid(artifact)
        expected = canonical_digest(delivery_art_content_projection(artifact))
        if artifact.get("integrity", {}).get("content_digest") != expected:
            raise DeliveryArtReadinessContractError(
                "artifact integrity digest does not match canonical content",
            )

    def _evaluate(
        self,
        prepared: PreparedReadinessRequest,
        context: _ResolvedReadinessContext,
    ) -> tuple[ReadinessFinding, ...]:
        subject = context.subject
        request = prepared.readiness_request
        findings: dict[str, ReadinessFinding] = {}

        def add(identifier: str, severity: str, summary: str, authority: str | None = DELIVERY_ART_AUTHORITY_REF) -> None:
            findings.setdefault(identifier, ReadinessFinding(identifier, severity, summary, authority))

        if _artifact_id(subject) != request["artifact_id"]:
            add("subject-id-mismatch", "blocker", "The resolved subject identity differs from the request.")
        if subject.get("artifact_type") != request["artifact_type"]:
            add("subject-type-mismatch", "blocker", "The resolved subject type differs from the readiness level.")
        if subject.get("delivery_id") != request["delivery_id"]:
            add("delivery-scope-mismatch", "blocker", "The resolved subject belongs to another Delivery initiative.")
        if set(subject.get("covered_work_item_ids", [])) != set(request["covered_work_item_ids"]):
            add("work-item-coverage-mismatch", "blocker", "The resolved subject covers different ART work items.")
        if request["digest_kind"] == "artifact-content":
            actual_digest = subject.get("integrity", {}).get("content_digest")
        else:
            actual_digest = subject.get("readiness", {}).get("subject_digest")
        if actual_digest != request["digest"]:
            add("subject-digest-mismatch", "blocker", "The resolved readiness subject digest differs from the request.")

        level = request["readiness_level"]
        if level == "architecture-ready":
            self._evaluate_architecture(subject, add)
        elif level == "implementation-ready":
            self._evaluate_work_start(subject, context, add)
        elif level == "merge-ready":
            self._evaluate_merge_ready(subject, context, add)
        else:
            self._evaluate_operating_ready(subject, context, add)
        return tuple(findings[key] for key in sorted(findings))

    @staticmethod
    def _evaluate_architecture(
        subject: dict[str, Any],
        add: Callable[[str, str, str, str | None], None],
    ) -> None:
        if subject.get("decision", {}).get("status") != "architecture-ready":
            add("architecture-decision-blocked", "blocker", "The architecture decision is not architecture-ready.")
        open_decisions = [
            entry
            for entry in subject.get("architecture", {}).get("contradictions_open_decisions", [])
            if entry.get("status") != "resolved"
        ]
        if open_decisions:
            add("architecture-decisions-open", "blocker", "The architecture packet still has unresolved decisions.")
        plan = subject.get("conformance_plan", {})
        if plan.get("required") and not plan.get("cases"):
            add("conformance-plan-empty", "blocker", "The required conformance plan has no executable cases.")

    def _evaluate_work_start(
        self,
        subject: dict[str, Any],
        context: _ResolvedReadinessContext,
        add: Callable[[str, str, str, str | None], None],
    ) -> None:
        readiness = subject.get("readiness", {})
        blockers = readiness.get("blockers", [])
        if readiness.get("level") != "implementation-ready" or blockers:
            add("implementation-readiness-blocked", "blocker", "The work-start record has unresolved implementation blockers.")
        if set(subject.get("invalidation_inputs", [])) != _REQUIRED_INVALIDATION_INPUTS:
            add("invalidation-set-incomplete", "blocker", "The work-start invalidation set is incomplete.")
        landing = subject.get("landing_unit", {})
        owner_repos = set(landing.get("owner_repos", []))
        branch_plan = landing.get("branch_plan", [])
        source_revisions = subject.get("source_snapshot", {}).get("repo_revisions", [])
        if owner_repos != {entry.get("repo") for entry in branch_plan} or owner_repos != {
            entry.get("repo") for entry in source_revisions
        }:
            add("landing-unit-source-scope-mismatch", "blocker", "Landing Unit owners, branch plan, and source snapshot differ.")
        revisions = {entry.get("repo"): entry for entry in source_revisions}
        for branch in branch_plan:
            revision = revisions.get(branch.get("repo"))
            if revision is None or (
                branch.get("base_ref") != revision.get("base_ref")
                or branch.get("base_commit") != revision.get("commit")
            ):
                add("landing-unit-base-mismatch", "blocker", "A Landing Unit base does not match its captured source revision.")
        architecture = subject.get("architecture", {})
        if architecture.get("required"):
            ref = architecture.get("packet_ref")
            resolved = context.source_artifacts.get(ref)
            if (
                resolved is None
                or resolved.get("integrity", {}).get("content_digest") != architecture.get("packet_digest")
                or resolved.get("decision", {}).get("status") != "architecture-ready"
            ):
                add("architecture-binding-invalid", "blocker", "The work-start record does not resolve an architecture-ready packet.")

    def _evaluate_merge_ready(
        self,
        subject: dict[str, Any],
        context: _ResolvedReadinessContext,
        add: Callable[[str, str, str, str | None], None],
    ) -> None:
        if subject.get("status") != "merge-ready" or subject.get("readiness", {}).get("level") != "merge-ready":
            add("merge-state-invalid", "blocker", "The Review Packet has not reached merge-ready state.")
        self._evaluate_review_evidence(subject, add)
        work_start = self._resolved_work_start(subject, context)
        if work_start is None or work_start.get("readiness", {}).get("level") != "implementation-ready":
            add("work-start-binding-invalid", "blocker", "The Review Packet does not resolve an implementation-ready work-start record.")
            return
        self._evaluate_work_start(work_start, context, add)
        architecture = self._resolved_architecture(work_start, context)
        if architecture is not None:
            self._evaluate_conformance(subject, architecture, add)

    def _evaluate_operating_ready(
        self,
        subject: dict[str, Any],
        context: _ResolvedReadinessContext,
        add: Callable[[str, str, str, str | None], None],
    ) -> None:
        candidate = context.candidate
        if candidate is None:
            add("finalization-candidate-missing", "blocker", "Operating readiness requires an OOS finalization candidate.")
            return
        if (
            candidate.get("status") != "draft"
            or candidate.get("readiness", {}).get("level") != "implementation-ready"
            or candidate.get("readiness", {}).get("receipt_refs")
        ):
            add("finalization-candidate-state-invalid", "blocker", "The OOS finalization candidate is not in the expected pre-receipt state.")
        if candidate.get("landing_unit", {}).get("evidence_kind") not in {
            "merged_pr",
            "approved_direct_land",
        }:
            add("merged-evidence-missing", "blocker", "Operating readiness requires merged or approved direct-land evidence.")
        for repo in candidate.get("landing_unit", {}).get("repos", []):
            if not _IMPLEMENTATION_REF_PATTERN.fullmatch(str(repo.get("merge_commit", ""))):
                add("merge-commit-missing", "blocker", "Every source repo requires an exact merge commit.")
        self._evaluate_review_evidence(candidate, add)
        predecessor_ref = candidate.get("custody", {}).get("supersedes")
        predecessor = context.source_artifacts.get(
            predecessor_ref.get("uri") if isinstance(predecessor_ref, dict) else "",
        )
        if predecessor is None or predecessor.get("status") != "merge-ready":
            add("merge-ready-predecessor-missing", "blocker", "The finalization candidate does not resolve its merge-ready predecessor.")
        else:
            self._evaluate_predecessor_preservation(candidate, predecessor, add)
        work_start = self._resolved_work_start(candidate, context)
        if work_start is None:
            add("work-start-binding-invalid", "blocker", "The finalization candidate does not resolve its work-start record.")
            return
        architecture = self._resolved_architecture(work_start, context)
        if architecture is not None:
            self._evaluate_conformance(candidate, architecture, add)

    @staticmethod
    def _evaluate_review_evidence(
        packet: dict[str, Any],
        add: Callable[[str, str, str, str | None], None],
    ) -> None:
        evidence = packet.get("evidence", {})
        records = [entry for section in _EVIDENCE_SECTIONS for entry in evidence.get(section, [])]
        if any(entry.get("result") == "fail" for entry in records):
            add("required-evidence-failed", "blocker", "At least one required evidence result failed.")
        repos = {entry.get("repo_name"): entry for entry in packet.get("landing_unit", {}).get("repos", [])}
        for record in records:
            if record.get("result") != "pass":
                continue
            for revision in record.get("source_revisions", []):
                repo = repos.get(revision.get("repo"))
                if repo is None or repo.get("head_commit") != revision.get("commit"):
                    add("evidence-source-head-mismatch", "blocker", "Passing evidence does not bind the exact reviewed source head.")
        evidence_ids = {
            entry.get("id")
            for section in ("changed_surfaces", *_EVIDENCE_SECTIONS)
            for entry in evidence.get(section, [])
        }
        mappings = evidence.get("acceptance_mapping", [])
        if {entry.get("work_item_id") for entry in mappings} != set(packet.get("covered_work_item_ids", [])):
            add("acceptance-coverage-incomplete", "blocker", "Acceptance mapping does not exactly cover the Review Packet work items.")
        if any(not set(entry.get("evidence_ids", [])).issubset(evidence_ids) for entry in mappings):
            add("acceptance-evidence-unresolved", "blocker", "Acceptance mapping references unknown evidence.")

    @staticmethod
    def _evaluate_conformance(
        packet: dict[str, Any],
        architecture: dict[str, Any],
        add: Callable[[str, str, str, str | None], None],
    ) -> None:
        covered = set(packet.get("covered_work_item_ids", []))
        cases = [
            case
            for case in architecture.get("conformance_plan", {}).get("cases", [])
            if case.get("target_readiness") == "merge-ready"
            and set(case.get("applies_to_work_item_ids", [])).intersection(covered)
        ]
        evidence = [
            entry
            for section in _EVIDENCE_SECTIONS
            for entry in packet.get("evidence", {}).get(section, [])
            if entry.get("result") == "pass"
        ]
        for case in cases:
            matches = [
                entry
                for entry in evidence
                if case.get("id") in entry.get("conformance_case_ids", [])
                and entry.get("fidelity") == case.get("fidelity")
            ]
            if not matches:
                add("conformance-evidence-incomplete", "blocker", "Applicable architecture conformance cases lack passing evidence at the planned fidelity.")
                return

    @staticmethod
    def _evaluate_predecessor_preservation(
        candidate: dict[str, Any],
        predecessor: dict[str, Any],
        add: Callable[[str, str, str, str | None], None],
    ) -> None:
        for field in ("packet_id", "delivery_id", "covered_work_item_ids", "created_at", "operator", "work_start"):
            if candidate.get(field) != predecessor.get(field):
                add("merge-ready-evidence-rewritten", "blocker", "The finalization candidate rewrites merge-ready identity or scope evidence.")
                return
        prior_landing = predecessor.get("landing_unit", {})
        current_landing = candidate.get("landing_unit", {})
        for field in ("decision", "rollback_boundary"):
            if current_landing.get(field) != prior_landing.get(field):
                add("merge-ready-evidence-rewritten", "blocker", "The finalization candidate rewrites its reviewed Landing Unit.")
                return
        current_repos = {entry.get("repo_name"): entry for entry in current_landing.get("repos", [])}
        for prior_repo in prior_landing.get("repos", []):
            current = current_repos.get(prior_repo.get("repo_name"))
            if current is None:
                add("merge-ready-evidence-rewritten", "blocker", "The finalization candidate drops a reviewed source repo.")
                return
            for field in (
                "repo_name",
                "branch",
                "base_ref",
                "base_commit",
                "head_commit",
                "pr_url",
                "changed_files",
                "change_record_refs",
            ):
                if current.get(field) != prior_repo.get(field):
                    add("merge-ready-evidence-rewritten", "blocker", "The finalization candidate rewrites reviewed source evidence.")
                    return
        for section in ("changed_surfaces", *_EVIDENCE_SECTIONS, "acceptance_mapping"):
            current_by_id = {
                entry.get("id", entry.get("work_item_id")): entry
                for entry in candidate.get("evidence", {}).get(section, [])
            }
            for entry in predecessor.get("evidence", {}).get(section, []):
                key = entry.get("id", entry.get("work_item_id"))
                if current_by_id.get(key) != entry:
                    add("merge-ready-evidence-rewritten", "blocker", "The finalization candidate rewrites reviewed evidence.")
                    return

    @staticmethod
    def _resolved_work_start(
        packet: dict[str, Any],
        context: _ResolvedReadinessContext,
    ) -> dict[str, Any] | None:
        work_start = packet.get("work_start", {})
        resolved = context.source_artifacts.get(str(work_start.get("artifact_ref", "")))
        if resolved is None or resolved.get("integrity", {}).get("content_digest") != work_start.get("artifact_digest"):
            return None
        return resolved

    @staticmethod
    def _resolved_architecture(
        work_start: dict[str, Any],
        context: _ResolvedReadinessContext,
    ) -> dict[str, Any] | None:
        architecture = work_start.get("architecture", {})
        if not architecture.get("required"):
            return None
        resolved = context.source_artifacts.get(str(architecture.get("packet_ref", "")))
        if resolved is None or resolved.get("integrity", {}).get("content_digest") != architecture.get("packet_digest"):
            return None
        return resolved

    def _persist(
        self,
        prepared: PreparedReadinessRequest,
        context: _ResolvedReadinessContext,
        findings: tuple[ReadinessFinding, ...],
        *,
        actor: str,
    ) -> DeliveryArtReadinessResult:
        request = prepared.readiness_request
        outcome = _readiness_outcome(findings)
        findings_records = [finding.to_record() for finding in findings]
        decision_key = canonical_digest(
            {
                "schema_version": 1,
                "profile_id": prepared.profile_id,
                "readiness_request": request,
                "issuer_implementation_ref": self._implementation_ref,
                "outcome": outcome,
                "findings": findings_records,
            },
        )
        with self._sessions() as session:
            existing = session.scalar(
                select(DeliveryArtReadinessReceipt).where(
                    DeliveryArtReadinessReceipt.decision_key == decision_key,
                ),
            )
            if existing is not None:
                result = self._materialize(existing, resolution="reused")
                self._append_ledger(actor, "delivery-art.readiness.reused", result)
                return result

        with self._sessions() as session:
            prior = session.scalar(
                select(DeliveryArtReadinessReceipt)
                .where(
                    DeliveryArtReadinessReceipt.delivery_id == request["delivery_id"],
                    DeliveryArtReadinessReceipt.subject_artifact_type == request["artifact_type"],
                    DeliveryArtReadinessReceipt.subject_artifact_id == request["artifact_id"],
                    DeliveryArtReadinessReceipt.readiness_level == request["readiness_level"],
                    DeliveryArtReadinessReceipt.profile_id == prepared.profile_id,
                )
                .order_by(desc(DeliveryArtReadinessReceipt.generation))
                .limit(1),
            )
            if prior is not None:
                session.expunge(prior)

        evaluated_at = self._evaluation_time(context, prior)
        persisted_at = evaluated_at + timedelta(seconds=1)
        token = hashlib.sha256(
            f"delivery-art-readiness\0{decision_key}".encode("utf-8"),
        ).hexdigest()[:24]
        receipt_id = f"art-readiness-receipt:{token}"
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "artifact_type": "delivery_art_readiness_receipt",
            "receipt_id": receipt_id,
            "delivery_id": request["delivery_id"],
            "covered_work_item_ids": copy.deepcopy(request["covered_work_item_ids"]),
            "subject": {
                "artifact_type": request["artifact_type"],
                "artifact_id": request["artifact_id"],
                "digest_kind": request["digest_kind"],
                "digest": request["digest"],
            },
            "readiness": {
                "level": request["readiness_level"],
                "outcome": outcome,
                "mutation_allowed": outcome == "ready",
                "evaluated_at": _timestamp(evaluated_at),
                "profile_id": prepared.profile_id,
                "target_scope": f"art:{request['delivery_id']}",
            },
            "issuer": {
                "owner_repo": "workspace-governance-control-fabric",
                "service_identity_ref": self._service_identity_ref,
                "implementation_ref": self._implementation_ref,
            },
            "findings": findings_records,
            "integrity": {
                "canonicalization": "RFC8785",
                "algorithm": "sha256",
            },
            "custody": {
                "state": "durable",
                "backend": "wgcf-receipt-ledger",
                "uri": "pending",
                "persisted_at": _timestamp(persisted_at),
                "supersedes": (
                    {"uri": prior.receipt_uri, "digest": prior.receipt_digest}
                    if prior is not None
                    else None
                ),
            },
        }
        receipt_digest = canonical_digest(delivery_art_content_projection(receipt))
        receipt["integrity"]["content_digest"] = receipt_digest
        receipt["custody"]["uri"] = (
            "wgcf://receipts/art-readiness/"
            f"art-readiness-receipt-{token}-{receipt_digest.removeprefix('sha256:')}.json"
        )
        self._contracts.require_valid(receipt)

        row = DeliveryArtReadinessReceipt(
            receipt_id=receipt_id,
            receipt_uri=receipt["custody"]["uri"],
            receipt_digest=receipt_digest,
            decision_key=decision_key,
            delivery_id=request["delivery_id"],
            subject_artifact_type=request["artifact_type"],
            subject_artifact_id=request["artifact_id"],
            subject_digest_kind=request["digest_kind"],
            subject_digest=request["digest"],
            readiness_level=request["readiness_level"],
            profile_id=prepared.profile_id,
            implementation_ref=self._implementation_ref,
            generation=1 if prior is None else prior.generation + 1,
            outcome=outcome,
            receipt=receipt,
            evaluated_at=evaluated_at,
            persisted_at=persisted_at,
            supersedes_receipt_uri=prior.receipt_uri if prior is not None else None,
            supersedes_receipt_digest=prior.receipt_digest if prior is not None else None,
        )
        try:
            with self._sessions.begin() as session:
                session.add(row)
                session.add(
                    self._ledger_event(
                        actor,
                        "delivery-art.readiness.persisted",
                        receipt,
                        row.generation,
                    ),
                )
                session.flush()
        except IntegrityError as exc:
            with self._sessions() as session:
                existing = session.scalar(
                    select(DeliveryArtReadinessReceipt).where(
                        DeliveryArtReadinessReceipt.decision_key == decision_key,
                    ),
                )
                if existing is None:
                    raise DeliveryArtReadinessUnavailable(
                        "readiness subject advanced concurrently",
                    ) from exc
                result = self._materialize(existing, resolution="reused")
            self._append_ledger(actor, "delivery-art.readiness.reused", result)
            return result
        return self._materialize(row, resolution="created")

    def _evaluation_time(
        self,
        context: _ResolvedReadinessContext,
        prior: DeliveryArtReadinessReceipt | None,
    ) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise DeliveryArtReadinessUnavailable("readiness clock must be timezone-aware")
        value = value.astimezone(timezone.utc).replace(microsecond=0)
        timestamps = [value]
        for artifact in context.source_artifacts.values():
            persisted = artifact.get("custody", {}).get("persisted_at")
            if persisted:
                timestamps.append(_parse_timestamp(persisted) + timedelta(seconds=1))
        if prior is not None:
            timestamps.append(_as_utc(prior.persisted_at) + timedelta(seconds=1))
        return max(timestamps)

    def _materialize(
        self,
        row: DeliveryArtReadinessReceipt,
        *,
        resolution: str,
    ) -> DeliveryArtReadinessResult:
        receipt = copy.deepcopy(row.receipt)
        self._contracts.require_valid(receipt)
        digest = canonical_digest(delivery_art_content_projection(receipt))
        if (
            digest != row.receipt_digest
            or receipt.get("integrity", {}).get("content_digest") != row.receipt_digest
            or receipt.get("custody", {}).get("uri") != row.receipt_uri
            or receipt.get("receipt_id") != row.receipt_id
            or receipt.get("issuer", {}).get("implementation_ref") != row.implementation_ref
        ):
            raise DeliveryArtReadinessUnavailable("readiness receipt ledger integrity failed")
        expected_token = row.receipt_id.removeprefix("art-readiness-receipt:")
        expected_uri = (
            "wgcf://receipts/art-readiness/"
            f"art-readiness-receipt-{expected_token}-{row.receipt_digest.removeprefix('sha256:')}.json"
        )
        if row.receipt_uri != expected_uri:
            raise DeliveryArtReadinessUnavailable("readiness receipt is not content-addressed")
        return DeliveryArtReadinessResult(
            artifact=receipt,
            generation=row.generation,
            resolution=resolution,
        )

    def _append_ledger(
        self,
        actor: str,
        action: str,
        result: DeliveryArtReadinessResult,
    ) -> None:
        receipt = result.artifact
        with self._sessions.begin() as session:
            session.add(self._ledger_event(actor, action, receipt, result.generation))

    @staticmethod
    def _ledger_event(
        actor: str,
        action: str,
        receipt: dict[str, Any],
        generation: int,
    ) -> LedgerEvent:
        return LedgerEvent(
            event_id=f"ledger-event:delivery-art-readiness:{uuid4().hex}",
            actor=actor,
            action=action,
            target=receipt["custody"]["uri"],
            outcome=receipt["readiness"]["outcome"],
            receipt_refs=[
                {
                    "receipt_id": receipt["receipt_id"],
                    "uri": receipt["custody"]["uri"],
                    "digest": receipt["integrity"]["content_digest"],
                    "generation": generation,
                },
                {
                    "uri": _subject_uri(
                        receipt["subject"]["digest_kind"],
                        receipt["subject"]["digest"],
                    ),
                    "digest": receipt["subject"]["digest"],
                },
            ],
        )


def _require_source_ref(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"uri", "digest"}:
        raise DeliveryArtReadinessContractError(f"{label} must contain uri and digest")
    uri = value.get("uri")
    digest = value.get("digest")
    if not isinstance(uri, str) or not _SOURCE_URI_PATTERN.fullmatch(uri):
        raise DeliveryArtReadinessContractError(f"{label} URI is invalid")
    if not isinstance(digest, str) or not _DIGEST_PATTERN.fullmatch(digest):
        raise DeliveryArtReadinessContractError(f"{label} digest is invalid")
    if uri != _subject_uri("artifact-content", digest):
        raise DeliveryArtReadinessContractError(f"{label} URI and digest differ")
    return {"uri": uri, "digest": digest}


def _artifact_id(artifact: dict[str, Any]) -> Any:
    return artifact.get("packet_id") if artifact.get("artifact_type") == "art_review_packet" else artifact.get("artifact_id")


def _readiness_outcome(findings: tuple[ReadinessFinding, ...]) -> str:
    if any(finding.severity in {"blocker", "error"} for finding in findings):
        return "blocked"
    if findings:
        return "review_required"
    return "ready"


def _subject_uri(digest_kind: str, digest: str) -> str:
    prefix = "wgcf://artifacts/delivery-art/sha256" if digest_kind == "artifact-content" else "wgcf://readiness-subjects/delivery-art/sha256"
    return f"{prefix}/{digest.removeprefix('sha256:')}"


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DeliveryArtReadinessContractError("artifact timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise DeliveryArtReadinessContractError("artifact timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def build_delivery_art_readiness_runtime(
    artifact_registry: DeliveryArtifactRegistry,
) -> DeliveryArtReadinessService:
    """Build the fail-closed dev-integration readiness runtime."""

    if os.environ.get(RUNTIME_PROFILE_ENV, "").strip() != "dev-integration":
        raise DeliveryArtReadinessUnavailable(
            "Delivery ART readiness is enabled only in dev-integration",
        )
    service_identity_ref = os.environ.get(SERVICE_IDENTITY_ENV, "").strip()
    if not service_identity_ref:
        raise DeliveryArtReadinessUnavailable(
            "Delivery ART readiness service identity is not configured",
        )
    return DeliveryArtReadinessService(
        session_factory=create_session_factory(),
        artifact_registry=artifact_registry,
        service_identity_ref=service_identity_ref,
        implementation_ref=read_implementation_ref(),
    )
