"""Immutable, non-mutating repository custody readiness decisions."""

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
from .db.models import LedgerEvent, RepositoryCustodyDecisionRecord
from .repository_custody_contracts import (
    RepositoryCustodyContractBundle,
    RepositoryCustodyContractError,
)


MAX_REPOSITORY_CUSTODY_READINESS_REQUEST_BYTES = 65_536
_ALLOWED_PROFILE = "dev-integration"
_ACTIVATION_ENV = "WGCF_REPOSITORY_CUSTODY_READINESS_ENABLED"
_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{24}$")
_IMPLEMENTATION_REF_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class RepositoryCustodyReadinessError(RuntimeError):
    """Base failure for repository custody readiness."""


class RepositoryCustodyReadinessRequestError(RepositoryCustodyReadinessError):
    """The supplied request is malformed or violates its integrity contract."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RepositoryCustodyReadinessConflict(RepositoryCustodyReadinessError):
    """A request id was replayed with different content."""


class RepositoryCustodyReadinessNotFound(RepositoryCustodyReadinessError):
    """The requested immutable decision does not exist."""


class RepositoryCustodyReadinessUnavailable(RepositoryCustodyReadinessError):
    """The evaluator contract or decision ledger is unavailable."""


@dataclass(frozen=True)
class PreparedRepositoryCustodyRequest:
    request: dict[str, Any]
    request_id: str
    request_digest: str


@dataclass(frozen=True)
class RepositoryCustodyReadinessResult:
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


def prepare_repository_custody_request(
    raw_request: bytes,
    *,
    contracts: RepositoryCustodyContractBundle,
) -> PreparedRepositoryCustodyRequest:
    if len(raw_request) > MAX_REPOSITORY_CUSTODY_READINESS_REQUEST_BYTES:
        raise RepositoryCustodyReadinessRequestError(
            "request-too-large",
            "repository custody readiness request exceeds the payload limit",
        )
    try:
        request = strict_json_loads(raw_request)
    except (RecursionError, ValueError) as exc:
        raise RepositoryCustodyReadinessRequestError("malformed-request", str(exc)) from exc
    try:
        contracts.require_valid("repository_custody_request", request)
    except RepositoryCustodyContractError as exc:
        raise RepositoryCustodyReadinessRequestError("malformed-request", str(exc)) from exc

    projection = copy.deepcopy(request)
    supplied_digest = projection.pop("request_digest")
    expected_digest = canonical_digest(projection)
    if supplied_digest != expected_digest:
        raise RepositoryCustodyReadinessRequestError(
            "request-digest-mismatch",
            "repository custody request_digest does not match canonical request content",
        )
    return PreparedRepositoryCustodyRequest(
        request=request,
        request_id=request["request_id"],
        request_digest=supplied_digest,
    )


class RepositoryCustodyReadinessService:
    """Evaluate policy readiness without executing or recording custody."""

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
            raise RepositoryCustodyReadinessUnavailable(
                "repository custody readiness service identity is not configured",
            )
        if not _IMPLEMENTATION_REF_PATTERN.fullmatch(implementation_ref):
            raise RepositoryCustodyReadinessUnavailable(
                "repository custody readiness implementation_ref must be an exact Git commit",
            )
        try:
            self._contracts = contract_bundle or RepositoryCustodyContractBundle.load()
        except RepositoryCustodyContractError as exc:
            raise RepositoryCustodyReadinessUnavailable(
                "repository custody readiness contracts are unavailable",
            ) from exc
        self._sessions = session_factory
        self._service_identity_ref = service_identity_ref.strip()
        self._implementation_ref = implementation_ref
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(self, raw_request: bytes, *, actor: str) -> RepositoryCustodyReadinessResult:
        prepared = prepare_repository_custody_request(raw_request, contracts=self._contracts)
        try:
            existing = self._find_by_request_id(prepared.request_id)
            if existing is not None:
                if existing.request_digest != prepared.request_digest:
                    raise RepositoryCustodyReadinessConflict(
                        "repository custody request id is already bound to different content",
                    )
                result = self._materialize(existing, resolution="reused")
                self._append_ledger(actor, "repository-custody.readiness.reused", result)
                return result
            decision = self._evaluate(prepared)
            return self._persist(prepared, decision, actor=actor)
        except RepositoryCustodyReadinessError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryCustodyReadinessUnavailable(
                "repository custody readiness ledger is unavailable",
            ) from exc

    def read(self, decision_token: str, *, actor: str) -> RepositoryCustodyReadinessResult:
        if not _TOKEN_PATTERN.fullmatch(decision_token):
            raise RepositoryCustodyReadinessRequestError(
                "invalid-decision-token",
                "repository custody decision token is invalid",
            )
        decision_id = f"repository-custody-decision:{decision_token}"
        try:
            with self._sessions() as session:
                row = session.get(RepositoryCustodyDecisionRecord, decision_id)
                if row is None:
                    raise RepositoryCustodyReadinessNotFound(
                        "repository custody readiness decision was not found",
                    )
                result = self._materialize(row, resolution="read")
            self._append_ledger(actor, "repository-custody.readiness.read", result)
            return result
        except RepositoryCustodyReadinessError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryCustodyReadinessUnavailable(
                "repository custody readiness ledger is unavailable",
            ) from exc

    def _evaluate(self, prepared: PreparedRepositoryCustodyRequest) -> dict[str, Any]:
        request = prepared.request
        findings: list[dict[str, str]] = []
        policy_ref = request["authority"]["policy_profile_ref"]
        approval_ref = request["authority"]["approval_ref"]
        credential_ref = request["authority"]["credential_binding_ref"]
        action = request["action"]

        if action not in self._contracts.readiness_capabilities:
            findings.append(
                _finding(
                    "custody-action-not-active",
                    "blocking",
                    "The requested custody action is not active in this readiness surface.",
                ),
            )
        if action == "provision-new":
            scope = self._contracts.provisioning_scope
            target = request["target"]
            if (
                target["provider"] != scope["provider"]
                or target["provider_host"] != scope["provider_host"]
                or target["owner_scope"] != scope["owner_scope"]
            ):
                findings.append(
                    _finding(
                        "custody-provisioning-provider-not-active",
                        "blocking",
                        "Repository provisioning is active only for the approved GitHub organization scope.",
                    ),
                )
        if (
            policy_ref["uri"] != self._contracts.authority_uri
            or policy_ref["digest"] != self._contracts.authority_digest
        ):
            findings.append(
                _finding(
                    "custody-policy-binding-stale",
                    "blocking",
                    "The request is not bound to the current repository custody authority.",
                ),
            )
        if approval_ref is None:
            findings.append(
                _finding(
                    "custody-approval-missing",
                    "blocking",
                    "The custody-changing request has no exact operator approval reference.",
                ),
            )
        for label, artifact_ref in (
            ("operator", request["operator_ref"]),
            ("approval", approval_ref),
            ("credential", credential_ref),
        ):
            if artifact_ref is not None and _artifact_ref_looks_secret_bearing(artifact_ref):
                findings.append(
                    _finding(
                        f"{label}-reference-secret-bearing",
                        "blocking",
                        f"The {label} reference may not carry credential material.",
                    ),
                )

        if findings:
            outcome = "denied"
            resolved_identity = None
            approved_provisioning = None
            next_action = "stop"
            obligations = ["preserve-current-custody-state", "emit-terminal-custody-receipt"]
        elif action == "provision-new":
            outcome = "allowed"
            resolved_identity = None
            approved_provisioning = {
                "provider": request["target"]["provider"],
                "provider_host": request["target"]["provider_host"],
                "owner": request["target"]["owner"],
                "owner_scope": request["target"]["owner_scope"],
                "name": request["target"]["name"],
                "settings": copy.deepcopy(request["provisioning"]),
            }
            next_action = "create-provider"
            findings = [
                _finding(
                    "repository-provisioning-request-ready",
                    "info",
                    "The organization target and explicit baseline settings are approved for provider creation.",
                ),
            ]
            obligations = [
                "verify-exact-operator-approval",
                "create-provider-once",
                "require-fresh-provider-readback",
                "verify-applied-provisioning-settings",
                "never-compensate-with-delete",
                "emit-terminal-custody-receipt",
            ]
        else:
            outcome = "allowed"
            resolved_identity = {
                "provider": request["target"]["provider"],
                "provider_repository_id": request["target"]["provider_repository_id"],
            }
            approved_provisioning = None
            next_action = "read-provider"
            findings = [
                _finding(
                    "repository-custody-request-ready",
                    "info",
                    "The request is current, policy-bound, and ready for provider readback.",
                ),
            ]
            obligations = [
                "verify-exact-operator-approval",
                "require-fresh-provider-readback",
                "preserve-immutable-provider-identity",
                "emit-terminal-custody-receipt",
            ]

        token = hashlib.sha256(
            f"repository-custody-readiness\0{prepared.request_id}\0{prepared.request_digest}".encode(
                "utf-8",
            ),
        ).hexdigest()[:24]
        decision: dict[str, Any] = {
            "schema_version": 1,
            "artifact_type": "repository_custody_decision",
            "decision_id": f"repository-custody-decision:{token}",
            "request_ref": {
                "uri": (
                    "wgcf://requests/repository-custody/"
                    f"{prepared.request_digest.removeprefix('sha256:')}.json"
                ),
                "digest": prepared.request_digest,
            },
            "evaluated_at": _timestamp(self._clock()),
            "policy_version": self._contracts.policy_version,
            "action": action,
            "outcome": outcome,
            "resolved_identity": resolved_identity,
            "approved_provisioning": approved_provisioning,
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
            self._contracts.require_valid("repository_custody_decision", decision)
        except RepositoryCustodyContractError as exc:
            raise RepositoryCustodyReadinessUnavailable(
                "repository custody readiness generated an invalid decision",
            ) from exc
        return decision

    def _persist(
        self,
        prepared: PreparedRepositoryCustodyRequest,
        decision: dict[str, Any],
        *,
        actor: str,
    ) -> RepositoryCustodyReadinessResult:
        request = prepared.request
        digest = decision["integrity"]["content_digest"]
        token = decision["decision_id"].removeprefix("repository-custody-decision:")
        decision_uri = (
            "wgcf://decisions/repository-custody/"
            f"repository-custody-decision-{token}-{digest.removeprefix('sha256:')}.json"
        )
        row = RepositoryCustodyDecisionRecord(
            decision_id=decision["decision_id"],
            decision_uri=decision_uri,
            decision_digest=digest,
            request_id=prepared.request_id,
            request_digest=prepared.request_digest,
            action=request["action"],
            provider=request["target"]["provider"],
            provider_repository_id=request["target"]["provider_repository_id"],
            workspace_owner_ref=request["requested_custody"]["workspace_owner_ref"],
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
                        "repository-custody.readiness.persisted",
                        decision,
                        decision_uri,
                    ),
                )
                session.flush()
        except IntegrityError as exc:
            existing = self._find_by_request_id(prepared.request_id)
            if existing is None:
                raise RepositoryCustodyReadinessUnavailable(
                    "repository custody readiness decision advanced concurrently",
                ) from exc
            if existing.request_digest != prepared.request_digest:
                raise RepositoryCustodyReadinessConflict(
                    "repository custody request id is already bound to different content",
                ) from exc
            result = self._materialize(existing, resolution="reused")
            self._append_ledger(actor, "repository-custody.readiness.reused", result)
            return result
        return self._materialize(row, resolution="created")

    def _find_by_request_id(self, request_id: str) -> RepositoryCustodyDecisionRecord | None:
        with self._sessions() as session:
            row = session.scalar(
                select(RepositoryCustodyDecisionRecord).where(
                    RepositoryCustodyDecisionRecord.request_id == request_id,
                ),
            )
            if row is not None:
                session.expunge(row)
            return row

    def _materialize(
        self,
        row: RepositoryCustodyDecisionRecord,
        *,
        resolution: str,
    ) -> RepositoryCustodyReadinessResult:
        decision = copy.deepcopy(row.decision)
        try:
            self._contracts.require_valid("repository_custody_decision", decision)
        except RepositoryCustodyContractError as exc:
            raise RepositoryCustodyReadinessUnavailable(
                "repository custody readiness decision ledger integrity failed",
            ) from exc
        digest = canonical_digest(_content_projection(decision))
        token = row.decision_id.removeprefix("repository-custody-decision:")
        expected_uri = (
            "wgcf://decisions/repository-custody/"
            f"repository-custody-decision-{token}-{row.decision_digest.removeprefix('sha256:')}.json"
        )
        if (
            decision["decision_id"] != row.decision_id
            or decision["request_ref"]["digest"] != row.request_digest
            or decision["action"] != row.action
            or decision["outcome"] != row.outcome
            or decision["policy_version"] != self._contracts.policy_version
            or decision["integrity"]["content_digest"] != row.decision_digest
            or digest != row.decision_digest
            or row.decision_uri != expected_uri
        ):
            raise RepositoryCustodyReadinessUnavailable(
                "repository custody readiness decision ledger integrity failed",
            )
        return RepositoryCustodyReadinessResult(decision, resolution, row.decision_uri)

    def _append_ledger(
        self,
        actor: str,
        action: str,
        result: RepositoryCustodyReadinessResult,
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
            event_id=f"ledger-event:repository-custody-readiness:{uuid4().hex}",
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
        raise RepositoryCustodyReadinessUnavailable(
            "repository custody readiness clock must be timezone-aware",
        )
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def build_repository_custody_readiness_runtime() -> RepositoryCustodyReadinessService:
    if os.environ.get(RUNTIME_PROFILE_ENV, "").strip() != _ALLOWED_PROFILE:
        raise RepositoryCustodyReadinessUnavailable(
            "repository custody readiness is enabled only in dev-integration",
        )
    if os.environ.get(_ACTIVATION_ENV, "").strip().lower() != "true":
        raise RepositoryCustodyReadinessUnavailable(
            "repository custody readiness has not been activated",
        )
    contracts = RepositoryCustodyContractBundle.load()
    if contracts.authority["runtime_activation"]["enabled"] is not True:
        raise RepositoryCustodyReadinessUnavailable(
            "repository custody authority has not activated the runtime",
        )
    return RepositoryCustodyReadinessService(
        session_factory=create_session_factory(),
        service_identity_ref=os.environ.get(SERVICE_IDENTITY_ENV, "").strip(),
        implementation_ref=read_implementation_ref(),
        contract_bundle=contracts,
    )
