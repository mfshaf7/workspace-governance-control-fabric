"""Immutable, non-mutating repository lifecycle readiness decisions."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
import re
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .artifact_registry import RUNTIME_PROFILE_ENV, SERVICE_IDENTITY_ENV, read_implementation_ref
from .canonical_json import canonical_digest, strict_json_loads
from .database import create_session_factory
from .db.models import LedgerEvent, RepositoryLifecycleDecisionRecord
from .repository_custody_contracts import (
    RepositoryCustodyContractBundle,
    RepositoryCustodyContractError,
)


MAX_REPOSITORY_LIFECYCLE_READINESS_REQUEST_BYTES = 65_536
_ALLOWED_PROFILE = "dev-integration"
_ACTIVATION_ENV = "WGCF_REPOSITORY_LIFECYCLE_READINESS_ENABLED"
_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{24}$")
_IMPLEMENTATION_REF_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_PROVIDER_ACTIONS = frozenset({"archive-provider", "unarchive-provider"})


class RepositoryLifecycleReadinessError(RuntimeError):
    """Base failure for repository lifecycle readiness."""


class RepositoryLifecycleReadinessRequestError(RepositoryLifecycleReadinessError):
    """The supplied request is malformed or violates its integrity contract."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RepositoryLifecycleReadinessConflict(RepositoryLifecycleReadinessError):
    """A request id was replayed with different content."""


class RepositoryLifecycleReadinessNotFound(RepositoryLifecycleReadinessError):
    """The requested immutable decision does not exist."""


class RepositoryLifecycleReadinessUnavailable(RepositoryLifecycleReadinessError):
    """The evaluator contract or decision ledger is unavailable."""


@dataclass(frozen=True)
class PreparedRepositoryLifecycleRequest:
    request: dict[str, Any]
    request_id: str
    request_digest: str


@dataclass(frozen=True)
class RepositoryLifecycleReadinessResult:
    decision: dict[str, Any]
    resolution: str
    decision_uri: str

    def to_record(self) -> dict[str, Any]:
        return {
            "decision": copy.deepcopy(self.decision),
            "ledger": {
                "resolution": self.resolution,
                "state": "durable",
                "ref": {
                    "uri": self.decision_uri,
                    "digest": self.decision["integrity"]["content_digest"],
                },
            },
        }


def prepare_repository_lifecycle_request(
    raw_request: bytes,
    *,
    contracts: RepositoryCustodyContractBundle,
) -> PreparedRepositoryLifecycleRequest:
    if len(raw_request) > MAX_REPOSITORY_LIFECYCLE_READINESS_REQUEST_BYTES:
        raise RepositoryLifecycleReadinessRequestError(
            "request-too-large",
            "repository lifecycle readiness request exceeds the payload limit",
        )
    try:
        request = strict_json_loads(raw_request)
    except (RecursionError, ValueError) as exc:
        raise RepositoryLifecycleReadinessRequestError("malformed-request", str(exc)) from exc
    try:
        contracts.require_valid("repository_lifecycle_request", request)
    except RepositoryCustodyContractError as exc:
        raise RepositoryLifecycleReadinessRequestError("malformed-request", str(exc)) from exc

    projection = copy.deepcopy(request)
    supplied_digest = projection.pop("request_digest")
    expected_digest = canonical_digest(projection)
    if supplied_digest != expected_digest:
        raise RepositoryLifecycleReadinessRequestError(
            "request-digest-mismatch",
            "repository lifecycle request_digest does not match canonical request content",
        )
    return PreparedRepositoryLifecycleRequest(
        request=request,
        request_id=request["request_id"],
        request_digest=supplied_digest,
    )


class RepositoryLifecycleReadinessService:
    """Evaluate one lifecycle transition without executing any mutation."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        service_identity_ref: str,
        implementation_ref: str,
        contract_bundle: RepositoryCustodyContractBundle | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not service_identity_ref.strip():
            raise RepositoryLifecycleReadinessUnavailable(
                "repository lifecycle readiness service identity is not configured",
            )
        if not _IMPLEMENTATION_REF_PATTERN.fullmatch(implementation_ref):
            raise RepositoryLifecycleReadinessUnavailable(
                "repository lifecycle readiness implementation_ref must be an exact Git commit",
            )
        try:
            self._contracts = contract_bundle or RepositoryCustodyContractBundle.load()
        except RepositoryCustodyContractError as exc:
            raise RepositoryLifecycleReadinessUnavailable(
                "repository lifecycle readiness contracts are unavailable",
            ) from exc
        self._sessions = session_factory
        self._service_identity_ref = service_identity_ref.strip()
        self._implementation_ref = implementation_ref
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(self, raw_request: bytes, *, actor: str) -> RepositoryLifecycleReadinessResult:
        prepared = prepare_repository_lifecycle_request(raw_request, contracts=self._contracts)
        try:
            existing = self._find_by_request_id(prepared.request_id)
            if existing is not None:
                if existing.request_digest != prepared.request_digest:
                    raise RepositoryLifecycleReadinessConflict(
                        "repository lifecycle request id is already bound to different content",
                    )
                result = self._materialize(existing, resolution="reused")
                self._append_ledger(actor, "repository-lifecycle.readiness.reused", result)
                return result
            decision = self._evaluate(prepared)
            return self._persist(prepared, decision, actor=actor)
        except RepositoryLifecycleReadinessError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryLifecycleReadinessUnavailable(
                "repository lifecycle readiness ledger is unavailable",
            ) from exc

    def read(self, decision_token: str, *, actor: str) -> RepositoryLifecycleReadinessResult:
        if not _TOKEN_PATTERN.fullmatch(decision_token):
            raise RepositoryLifecycleReadinessRequestError(
                "invalid-decision-token",
                "repository lifecycle decision token is invalid",
            )
        decision_id = f"repository-lifecycle-decision:{decision_token}"
        try:
            with self._sessions() as session:
                row = session.get(RepositoryLifecycleDecisionRecord, decision_id)
                if row is None:
                    raise RepositoryLifecycleReadinessNotFound(
                        "repository lifecycle readiness decision was not found",
                    )
                result = self._materialize(row, resolution="read")
            self._append_ledger(actor, "repository-lifecycle.readiness.read", result)
            return result
        except RepositoryLifecycleReadinessError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryLifecycleReadinessUnavailable(
                "repository lifecycle readiness ledger is unavailable",
            ) from exc

    def _evaluate(self, prepared: PreparedRepositoryLifecycleRequest) -> dict[str, Any]:
        request = prepared.request
        action = request["action"]
        current_state = request["current_state"]
        impact = request["impact"]
        authority = request["authority"]
        findings: list[dict[str, str]] = []

        policy_ref = authority["policy_profile_ref"]
        if (
            policy_ref["uri"] != self._contracts.authority_uri
            or policy_ref["digest"] != self._contracts.authority_digest
        ):
            findings.append(
                _finding(
                    "repository-lifecycle-policy-binding-stale",
                    "blocking",
                    "The request is not bound to the current repository lifecycle authority.",
                ),
            )

        for label, artifact_ref in _request_artifact_refs(request):
            if artifact_ref is not None and _artifact_ref_looks_secret_bearing(artifact_ref):
                findings.append(
                    _finding(
                        f"{label}-reference-secret-bearing",
                        "blocking",
                        f"The {label.replace('-', ' ')} reference may not carry credential material.",
                    ),
                )

        if impact["blocking_finding_count"] > impact["finding_count"]:
            findings.append(
                _finding(
                    "repository-lifecycle-impact-count-invalid",
                    "blocking",
                    "Blocking findings cannot exceed the total impact finding count.",
                ),
            )

        if action in _PROVIDER_ACTIONS:
            if request["repository_identity"]["provider"] != "github":
                findings.append(
                    _finding(
                        "repository-lifecycle-provider-not-active",
                        "blocking",
                        "Provider lifecycle mutation is active only for the approved GitHub authority.",
                    ),
                )
            if current_state["provider_lifecycle_state"] == "unavailable":
                findings.append(
                    _finding(
                        "repository-lifecycle-provider-unavailable",
                        "blocking",
                        "Provider truth is unavailable, so lifecycle mutation cannot be approved.",
                    ),
                )
            if current_state["provider_version"] is None:
                findings.append(
                    _finding(
                        "repository-lifecycle-provider-version-missing",
                        "blocking",
                        "Provider lifecycle mutation requires the current provider version.",
                    ),
                )

        if (
            action == "transfer-workspace-custody"
            and request["target"]["workspace_owner_ref"] == current_state["workspace_owner_ref"]
        ):
            findings.append(
                _finding(
                    "repository-lifecycle-target-owner-unchanged",
                    "blocking",
                    "The target workspace owner must differ from the current owner.",
                ),
            )

        disposition = impact["blocker_disposition"]
        if disposition is not None and disposition["decision"] == "defer":
            outcome = "requires-action"
            approved_target = None
            next_action = "request-correction"
            findings.append(
                _finding(
                    "repository-lifecycle-impact-deferred",
                    "warning",
                    "The impact disposition defers this lifecycle action.",
                ),
            )
        elif findings:
            outcome = "denied"
            approved_target = None
            next_action = "stop"
        else:
            outcome = "allowed"
            approved_target = copy.deepcopy(request["target"])
            next_action = _next_action(action)
            findings.append(
                _finding(
                    "repository-lifecycle-request-ready",
                    "info",
                    "The request is current, confirmed, impact-disposed, and ready for its exact next action.",
                ),
            )

        required_human_gates = ["exact-operator-approval"]
        if action == "transfer-workspace-custody":
            required_human_gates.extend(
                ["source-owner-acceptance", "target-owner-acceptance"],
            )
        elif action in _PROVIDER_ACTIONS:
            required_human_gates.append("governed-provider-credential-binding")

        obligations = [
            "preserve-immutable-repository-identity",
            "verify-current-custody-version",
            "do-not-mutate-downstream-consumers",
            "append-terminal-lifecycle-receipt",
            "preserve-immutable-history",
        ]
        if action in _PROVIDER_ACTIONS:
            obligations.extend(
                ["verify-current-provider-version", "require-fresh-provider-readback"],
            )
        else:
            obligations.append("do-not-mutate-provider-state")
        if request["reversal_of_receipt_ref"] is not None:
            obligations.append("preserve-reversed-receipt")

        token = hashlib.sha256(
            f"repository-lifecycle-readiness\0{prepared.request_id}\0{prepared.request_digest}".encode(
                "utf-8",
            ),
        ).hexdigest()[:24]
        decision: dict[str, Any] = {
            "schema_version": 1,
            "artifact_type": "repository_lifecycle_decision",
            "decision_id": f"repository-lifecycle-decision:{token}",
            "request_ref": {
                "uri": (
                    "wgcf://requests/repository-lifecycle/"
                    f"{prepared.request_digest.removeprefix('sha256:')}.json"
                ),
                "digest": prepared.request_digest,
            },
            "evaluated_at": _timestamp(self._clock()),
            "policy_version": self._contracts.lifecycle_policy_version,
            "action": action,
            "outcome": outcome,
            "current_state": copy.deepcopy(current_state),
            "approved_target": approved_target,
            "impact": {
                **copy.deepcopy(impact),
                "downstream_mutation": "none",
            },
            "required_human_gates": required_human_gates,
            "findings": findings,
            "obligations": obligations,
            "next_action": next_action,
            "integrity": {
                "canonicalization": "RFC8785",
                "algorithm": "sha256",
                "content_digest": "",
            },
        }
        decision["integrity"]["content_digest"] = canonical_digest(
            _content_projection(decision),
        )
        try:
            self._contracts.require_valid("repository_lifecycle_decision", decision)
        except RepositoryCustodyContractError as exc:
            raise RepositoryLifecycleReadinessUnavailable(
                "repository lifecycle readiness generated an invalid decision",
            ) from exc
        return decision

    def _persist(
        self,
        prepared: PreparedRepositoryLifecycleRequest,
        decision: dict[str, Any],
        *,
        actor: str,
    ) -> RepositoryLifecycleReadinessResult:
        request = prepared.request
        state = request["current_state"]
        digest = decision["integrity"]["content_digest"]
        token = decision["decision_id"].removeprefix("repository-lifecycle-decision:")
        decision_uri = (
            "wgcf://decisions/repository-lifecycle/"
            f"repository-lifecycle-decision-{token}-{digest.removeprefix('sha256:')}.json"
        )
        row = RepositoryLifecycleDecisionRecord(
            decision_id=decision["decision_id"],
            decision_uri=decision_uri,
            decision_digest=digest,
            request_id=prepared.request_id,
            request_digest=prepared.request_digest,
            action=request["action"],
            provider=request["repository_identity"]["provider"],
            provider_repository_id=request["repository_identity"]["provider_repository_id"],
            workspace_owner_ref=state["workspace_owner_ref"],
            custody_version=state["custody_version"],
            provider_version=state["provider_version"],
            policy_digest=self._contracts.authority_digest,
            implementation_ref=self._implementation_ref,
            outcome=decision["outcome"],
            decision=decision,
            evaluated_at=_parse_timestamp(decision["evaluated_at"]),
        )
        try:
            with self._sessions.begin() as session:
                session.add(row)
                session.add(
                    self._ledger_event(
                        actor,
                        "repository-lifecycle.readiness.persisted",
                        decision,
                        decision_uri,
                    ),
                )
                session.flush()
        except IntegrityError as exc:
            existing = self._find_by_request_id(prepared.request_id)
            if existing is None:
                raise RepositoryLifecycleReadinessUnavailable(
                    "repository lifecycle readiness decision advanced concurrently",
                ) from exc
            if existing.request_digest != prepared.request_digest:
                raise RepositoryLifecycleReadinessConflict(
                    "repository lifecycle request id is already bound to different content",
                ) from exc
            result = self._materialize(existing, resolution="reused")
            self._append_ledger(actor, "repository-lifecycle.readiness.reused", result)
            return result
        return self._materialize(row, resolution="created")

    def _find_by_request_id(self, request_id: str) -> RepositoryLifecycleDecisionRecord | None:
        with self._sessions() as session:
            row = session.scalar(
                select(RepositoryLifecycleDecisionRecord).where(
                    RepositoryLifecycleDecisionRecord.request_id == request_id,
                ),
            )
            if row is not None:
                session.expunge(row)
            return row

    def _materialize(
        self,
        row: RepositoryLifecycleDecisionRecord,
        *,
        resolution: str,
    ) -> RepositoryLifecycleReadinessResult:
        decision = copy.deepcopy(row.decision)
        try:
            self._contracts.require_valid("repository_lifecycle_decision", decision)
        except RepositoryCustodyContractError as exc:
            raise RepositoryLifecycleReadinessUnavailable(
                "repository lifecycle readiness decision ledger integrity failed",
            ) from exc
        digest = canonical_digest(_content_projection(decision))
        token = row.decision_id.removeprefix("repository-lifecycle-decision:")
        expected_uri = (
            "wgcf://decisions/repository-lifecycle/"
            f"repository-lifecycle-decision-{token}-{row.decision_digest.removeprefix('sha256:')}.json"
        )
        if (
            decision["decision_id"] != row.decision_id
            or decision["request_ref"]["digest"] != row.request_digest
            or decision["action"] != row.action
            or decision["outcome"] != row.outcome
            or decision["policy_version"] != self._contracts.lifecycle_policy_version
            or decision["integrity"]["content_digest"] != row.decision_digest
            or digest != row.decision_digest
            or row.decision_uri != expected_uri
        ):
            raise RepositoryLifecycleReadinessUnavailable(
                "repository lifecycle readiness decision ledger integrity failed",
            )
        return RepositoryLifecycleReadinessResult(decision, resolution, row.decision_uri)

    def _append_ledger(
        self,
        actor: str,
        action: str,
        result: RepositoryLifecycleReadinessResult,
    ) -> None:
        with self._sessions.begin() as session:
            session.add(self._ledger_event(actor, action, result.decision, result.decision_uri))

    def _ledger_event(
        self,
        actor: str,
        action: str,
        decision: dict[str, Any],
        decision_uri: str,
    ) -> LedgerEvent:
        return LedgerEvent(
            event_id=f"ledger-event:repository-lifecycle-readiness:{uuid4().hex}",
            actor=actor,
            action=action,
            target=decision_uri,
            outcome=decision["outcome"],
            receipt_refs=[
                {
                    "receipt_id": decision["decision_id"],
                    "uri": decision_uri,
                    "digest": decision["integrity"]["content_digest"],
                },
                {
                    "uri": self._contracts.authority_uri,
                    "digest": self._contracts.authority_digest,
                },
            ],
        )


def _request_artifact_refs(request: dict[str, Any]) -> tuple[tuple[str, dict[str, str] | None], ...]:
    authority = request["authority"]
    impact = request["impact"]
    disposition = impact["blocker_disposition"]
    return (
        ("operator", request["operator_ref"]),
        ("policy", authority["policy_profile_ref"]),
        ("approval", authority["approval_ref"]),
        ("source-owner-acceptance", authority["source_owner_acceptance_ref"]),
        ("target-owner-acceptance", authority["target_owner_acceptance_ref"]),
        ("provider-credential-binding", authority["provider_credential_binding_ref"]),
        ("impact-assessment", impact["impact_assessment_ref"]),
        ("blocker-evidence", disposition["evidence_ref"] if disposition is not None else None),
        ("reversal-receipt", request["reversal_of_receipt_ref"]),
    )


def _next_action(action: str) -> str:
    if action == "transfer-workspace-custody":
        return "apply-workspace-custody"
    return action


def _content_projection(artifact: dict[str, Any]) -> dict[str, Any]:
    projection = copy.deepcopy(artifact)
    projection["integrity"].pop("content_digest", None)
    return projection


def _artifact_ref_looks_secret_bearing(artifact_ref: dict[str, str]) -> bool:
    parsed = urlsplit(artifact_ref["uri"])
    return bool(parsed.username or parsed.password or parsed.query or parsed.fragment)


def _finding(code: str, severity: str, summary: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "summary": summary}


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise RepositoryLifecycleReadinessUnavailable(
            "repository lifecycle readiness clock must be timezone-aware",
        )
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def build_repository_lifecycle_readiness_runtime() -> RepositoryLifecycleReadinessService:
    if os.environ.get(RUNTIME_PROFILE_ENV, "").strip() != _ALLOWED_PROFILE:
        raise RepositoryLifecycleReadinessUnavailable(
            "repository lifecycle readiness is enabled only in dev-integration",
        )
    if os.environ.get(_ACTIVATION_ENV, "").strip().lower() != "true":
        raise RepositoryLifecycleReadinessUnavailable(
            "repository lifecycle readiness has not been activated",
        )
    contracts = RepositoryCustodyContractBundle.load()
    if contracts.authority["runtime_activation"]["enabled"] is not True:
        raise RepositoryLifecycleReadinessUnavailable(
            "repository lifecycle authority has not activated the runtime",
        )
    return RepositoryLifecycleReadinessService(
        session_factory=create_session_factory(),
        service_identity_ref=os.environ.get(SERVICE_IDENTITY_ENV, "").strip(),
        implementation_ref=read_implementation_ref(),
        contract_bundle=contracts,
    )
