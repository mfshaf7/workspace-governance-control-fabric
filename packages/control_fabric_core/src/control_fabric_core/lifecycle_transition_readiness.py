"""Non-mutating readiness and escalation evidence for lifecycle transitions."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import os
import re
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .artifact_registry import RUNTIME_PROFILE_ENV, SERVICE_IDENTITY_ENV, read_implementation_ref
from .canonical_json import canonical_digest, strict_json_loads
from .database import create_session_factory
from .db.models import EscalationRecord, LedgerEvent, LifecycleTransitionReadinessRecord
from .lifecycle_transition_contracts import (
    LifecycleTransitionContractBundle,
    LifecycleTransitionContractError,
)


MAX_LIFECYCLE_TRANSITION_EVALUATION_BYTES = 262_144
_ALLOWED_PROFILE = "dev-integration"
_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_ACTIVE_STATES = frozenset(
    {
        "prepared",
        "validating",
        "awaiting-authority",
        "awaiting-admission",
        "authorized",
        "applying",
    }
)
_TERMINAL_STATES = frozenset({"applied", "cancelled", "superseded"})
_ROUTE_DOMAINS = {
    "proposal-to-delivery": ("proposal", "delivery"),
    "proposal-to-prototype": ("proposal", "prototype"),
    "prototype-to-delivery": ("prototype", "delivery"),
}
_SECRET_QUERY_KEYS = frozenset(
    {"access_token", "api_key", "apikey", "key", "password", "secret", "token"}
)


class LifecycleTransitionReadinessError(RuntimeError):
    """Base failure for lifecycle-transition readiness."""


class LifecycleTransitionRequestError(LifecycleTransitionReadinessError):
    """The evaluation request is malformed or violates source integrity."""


class LifecycleTransitionConflict(LifecycleTransitionReadinessError):
    """An evaluation id already binds different content or a different caller."""


class LifecycleTransitionNotFound(LookupError, LifecycleTransitionReadinessError):
    """A caller-scoped lifecycle-transition readiness artifact was not found."""


class LifecycleTransitionUnavailable(LifecycleTransitionReadinessError):
    """The evaluator contract, identity, or ledger is unavailable."""


class LifecycleTransitionReadinessService:
    """Evaluate current OOS transition truth without mutating workflow state."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        service_identity_ref: str,
        implementation_ref: str,
        contract_bundle: LifecycleTransitionContractBundle | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not service_identity_ref.strip() or not _COMMIT.fullmatch(implementation_ref):
            raise LifecycleTransitionUnavailable(
                "lifecycle transition evaluator identity is incomplete",
            )
        try:
            self.contracts = contract_bundle or LifecycleTransitionContractBundle.load()
        except LifecycleTransitionContractError as exc:
            raise LifecycleTransitionUnavailable(
                "lifecycle transition readiness contracts are unavailable",
            ) from exc
        self.sessions = session_factory
        self.identity = service_identity_ref.strip()
        self.implementation = implementation_ref
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(self, raw: bytes, *, actor: str) -> dict[str, Any]:
        request = self._prepare(raw)
        try:
            with self.sessions() as session:
                existing = session.get(
                    LifecycleTransitionReadinessRecord,
                    request["evaluation_id"],
                )
                if existing is not None:
                    return self._reuse(existing, request, actor)

            evaluated_at = _utc(self.clock())
            readiness = self._evaluate(request, evaluated_at)
            row = LifecycleTransitionReadinessRecord(
                evaluation_id=request["evaluation_id"],
                evaluation_digest=request["evaluation_digest"],
                readiness_digest=readiness["readiness_digest"],
                actor=actor,
                transition_id=readiness["transition"]["transition_id"],
                source_revision=readiness["source"]["source_revision"],
                contract_digest=self.contracts.contract_digest,
                implementation_ref=self.implementation,
                outcome=readiness["outcome"],
                expires_at=_parse_time(readiness["valid_until"]),
                readiness=readiness,
            )
            try:
                with self.sessions.begin() as session:
                    session.add(row)
                    session.add(self._event(actor, "created", readiness))
                    escalation = readiness["escalation"]
                    if escalation is not None:
                        session.add(self._escalation(readiness, escalation))
                    session.flush()
            except IntegrityError:
                with self.sessions() as session:
                    existing = session.get(
                        LifecycleTransitionReadinessRecord,
                        request["evaluation_id"],
                    )
                    if existing is None:
                        raise LifecycleTransitionUnavailable(
                            "lifecycle transition readiness could not be persisted",
                        )
                    return self._reuse(existing, request, actor)
            return self._materialize(row, "created")
        except LifecycleTransitionReadinessError:
            raise
        except SQLAlchemyError as exc:
            raise LifecycleTransitionUnavailable(
                "lifecycle transition readiness ledger is unavailable",
            ) from exc

    def read(self, token: str, *, actor: str) -> dict[str, Any]:
        if not _TOKEN.fullmatch(token):
            raise LifecycleTransitionRequestError(
                "invalid lifecycle transition readiness token",
            )
        try:
            with self.sessions() as session:
                row = session.scalar(
                    select(LifecycleTransitionReadinessRecord).where(
                        LifecycleTransitionReadinessRecord.readiness_digest == f"sha256:{token}",
                        LifecycleTransitionReadinessRecord.actor == actor,
                    )
                )
                if row is None:
                    raise LifecycleTransitionNotFound(
                        "lifecycle transition readiness was not found",
                    )
                result = self._materialize(row, "read")
            with self.sessions.begin() as session:
                session.add(self._event(actor, "read", result["readiness"]))
            return result
        except LifecycleTransitionReadinessError:
            raise
        except SQLAlchemyError as exc:
            raise LifecycleTransitionUnavailable(
                "lifecycle transition readiness ledger is unavailable",
            ) from exc

    def _prepare(self, raw: bytes) -> dict[str, Any]:
        if len(raw) > MAX_LIFECYCLE_TRANSITION_EVALUATION_BYTES:
            raise LifecycleTransitionRequestError(
                "lifecycle transition evaluation exceeds the request limit",
            )
        try:
            request = strict_json_loads(raw)
            self.contracts.require_valid("evaluation", request)
            self.contracts.require_valid("projection", request["projection"])
        except (RecursionError, ValueError, LifecycleTransitionContractError) as exc:
            raise LifecycleTransitionRequestError(str(exc)) from exc
        if _artifact_digest(request, "evaluation_digest") != request["evaluation_digest"]:
            raise LifecycleTransitionRequestError(
                "lifecycle transition evaluation digest does not match canonical content",
            )
        return request

    def _evaluate(self, request: dict[str, Any], evaluated_at: datetime) -> dict[str, Any]:
        source = request["projection"]
        projection = source["projection"]
        findings: list[dict[str, str]] = []
        escalation: dict[str, Any] | None = None

        expected_domains = _ROUTE_DOMAINS[projection["route_id"]]
        observed_domains = (projection["source"]["domain"], projection["target"]["domain"])
        if observed_domains != expected_domains:
            findings.append(
                _finding(
                    "lifecycle-transition-route-binding-invalid",
                    "blocking",
                    "The transition route does not match its source and target domains.",
                    "operator-orchestration-service",
                )
            )

        freshness = source["freshness"]
        valid_until = _parse_time(freshness["valid_until"])
        if freshness["state"] != "current" or valid_until <= evaluated_at:
            findings.append(
                _finding(
                    "lifecycle-transition-source-not-current",
                    "blocking",
                    "The OOS transition projection is not current.",
                    "operator-orchestration-service",
                )
            )

        history = projection["history"]["entries"]
        sequences = [entry["sequence"] for entry in history]
        if (
            sequences != sorted(set(sequences))
            or sequences[-1] > source["revision"]["event_sequence"]
        ):
            findings.append(
                _finding(
                    "lifecycle-transition-history-order-invalid",
                    "blocking",
                    "Transition history does not agree with the source event sequence.",
                    "operator-orchestration-service",
                )
            )

        unsafe_refs = [ref for ref in _evidence_refs(projection) if not _safe_reference(ref)]
        if unsafe_refs:
            findings.append(
                _finding(
                    "lifecycle-transition-evidence-reference-unsafe",
                    "blocking",
                    "One or more transition evidence references contain unsafe credential material.",
                    "operator-orchestration-service",
                )
            )

        state = projection["state"]
        state_finding, state_escalation = _state_evaluation(projection)
        if state_finding is not None:
            findings.append(state_finding)
        if state_escalation is not None:
            escalation = state_escalation

        if any(finding["severity"] == "blocking" for finding in findings):
            outcome = "blocked"
            if escalation is None:
                escalation = {
                    "reason_code": findings[0]["code"],
                    "owner_ref": findings[0]["owner_ref"],
                    "required_fix": "Publish a corrected current OOS transition projection and retry readiness.",
                    "review_at": None,
                    "evidence_refs": [],
                }
        elif state in _TERMINAL_STATES:
            outcome = "terminal"
        elif state in {"returned", "deferred", "rejected", "failed"}:
            outcome = "requires-action"
        else:
            outcome = "ready"

        if not findings:
            findings.append(
                _finding(
                    "lifecycle-transition-current",
                    "info",
                    "The transition projection is current and internally consistent for its exact next action.",
                    "workspace-governance-control-fabric",
                )
            )

        safe_evidence_refs = sorted(
            {ref for ref in _evidence_refs(projection) if _safe_reference(ref)}
        )[:128]
        generated_valid_until = evaluated_at + timedelta(minutes=15)
        readiness = {
            "schema_version": 1,
            "artifact_type": "lifecycle-transition-readiness",
            "readiness_id": f"lifecycle-transition-readiness:{request['evaluation_id']}",
            "evaluated_at": _format_time(evaluated_at),
            "valid_until": _format_time(generated_valid_until),
            "transition": {
                "transition_id": projection["transition_id"],
                "route_id": projection["route_id"],
                "state": state,
                "correlation_id": projection["correlation_id"],
            },
            "source": {
                "record_ref": source["binding"]["record_ref"],
                "source_revision": source["revision"]["source_revision"],
                "event_sequence": source["revision"]["event_sequence"],
                "projection_digest": canonical_digest(source),
            },
            "outcome": outcome,
            "findings": findings,
            "escalation": escalation,
            "next_action": copy.deepcopy(projection["next_action"]),
            "evidence_refs": safe_evidence_refs,
            "authority": {
                "owner_ref": "workspace-governance-control-fabric",
                "contract_ref": self.contracts.authority_ref,
                "contract_digest": self.contracts.contract_digest,
            },
            "implementation_ref": self.implementation,
        }
        readiness["readiness_digest"] = canonical_digest(readiness)
        try:
            self.contracts.require_valid("readiness", readiness)
        except LifecycleTransitionContractError as exc:
            raise LifecycleTransitionUnavailable(
                "lifecycle transition evaluator generated an invalid readiness artifact",
            ) from exc
        return readiness

    def _reuse(
        self,
        row: LifecycleTransitionReadinessRecord,
        request: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        if row.evaluation_digest != request["evaluation_digest"] or row.actor != actor:
            raise LifecycleTransitionConflict(
                "lifecycle transition evaluation id already binds different content or caller",
            )
        result = self._materialize(row, "reused")
        with self.sessions.begin() as session:
            session.add(self._event(actor, "reused", result["readiness"]))
        return result

    def _materialize(
        self,
        row: LifecycleTransitionReadinessRecord,
        resolution: str,
    ) -> dict[str, Any]:
        readiness = copy.deepcopy(row.readiness)
        try:
            self.contracts.require_valid("readiness", readiness)
        except LifecycleTransitionContractError as exc:
            raise LifecycleTransitionUnavailable(
                "lifecycle transition readiness schema integrity failed",
            ) from exc
        if (
            _artifact_digest(readiness, "readiness_digest") != row.readiness_digest
            or readiness.get("readiness_digest") != row.readiness_digest
            or readiness.get("implementation_ref") != row.implementation_ref
            or readiness.get("authority", {}).get("contract_digest") != row.contract_digest
        ):
            raise LifecycleTransitionUnavailable(
                "lifecycle transition readiness integrity check failed",
            )
        return {
            "readiness": readiness,
            "ledger": {
                "resolution": resolution,
                "state": "durable",
                "ref": {
                    "uri": _readiness_uri(row.readiness_digest),
                    "digest": row.readiness_digest,
                },
            },
        }

    @staticmethod
    def _event(actor: str, resolution: str, readiness: dict[str, Any]) -> LedgerEvent:
        return LedgerEvent(
            event_id=f"ledger-event:lifecycle-transition:{uuid4().hex}",
            actor=actor,
            action=f"lifecycle-transition.readiness.{resolution}",
            target=_readiness_uri(readiness["readiness_digest"]),
            outcome=readiness["outcome"],
            receipt_refs=[
                {
                    "uri": _readiness_uri(readiness["readiness_digest"]),
                    "digest": readiness["readiness_digest"],
                }
            ],
        )

    @staticmethod
    def _escalation(
        readiness: dict[str, Any],
        escalation: dict[str, Any],
    ) -> EscalationRecord:
        digest = canonical_digest(
            {
                "readiness_id": readiness["readiness_id"],
                "escalation": escalation,
            }
        ).removeprefix("sha256:")
        return EscalationRecord(
            escalation_id=f"escalation:lifecycle-transition:{digest[:24]}",
            trigger_id=readiness["readiness_id"],
            target_system=escalation["owner_ref"],
            owner_repo=escalation["owner_ref"],
            required_record=escalation["reason_code"],
            evidence_refs=[{"uri": ref} for ref in escalation["evidence_refs"]],
            operator_action_required=escalation["required_fix"],
        )


def _state_evaluation(
    projection: dict[str, Any],
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    state = projection["state"]
    next_action = projection["next_action"]
    if state in _ACTIVE_STATES:
        if next_action is None:
            return _inconsistent_state(), None
        blocked_gate = next(
            (gate for gate in projection["validation"]["gates"] if gate["state"] == "blocked"),
            None,
        )
        if blocked_gate is not None:
            return (
                _finding(
                    "lifecycle-transition-validation-gate-blocked",
                    "blocking",
                    "A validation gate blocks the transition's exact next action.",
                    blocked_gate["owner_ref"],
                ),
                _gate_escalation(blocked_gate),
            )
        return None, None
    if state == "blocked":
        gate = projection["blocked_gate"]
        if gate is None:
            return _inconsistent_state(), None
        return (
            _finding(
                "lifecycle-transition-gate-blocked",
                "blocking",
                "The transition is blocked by an owner-routed gate.",
                gate["owner_ref"],
            ),
            _gate_escalation(gate),
        )
    if state == "returned":
        correction = projection["correction"]
        if correction is None:
            return _inconsistent_state(), None
        return (
            _finding(
                "lifecycle-transition-source-correction-required",
                "warning",
                "The source owner must correct the transition input before retry.",
                correction["owner_ref"],
            ),
            {
                "reason_code": correction["reason_code"],
                "owner_ref": correction["owner_ref"],
                "required_fix": correction["required_fix"],
                "review_at": next_action["review_at"] if next_action else None,
                "evidence_refs": [],
            },
        )
    if state == "deferred":
        deferred = projection["deferred"]
        if deferred is None or next_action is None:
            return _inconsistent_state(), None
        return (
            _finding(
                "lifecycle-transition-deferred",
                "warning",
                "The transition is deferred until its recorded review point.",
                next_action["owner_ref"],
            ),
            {
                "reason_code": deferred["reason_code"],
                "owner_ref": next_action["owner_ref"],
                "required_fix": deferred["justification"],
                "review_at": deferred["review_at"],
                "evidence_refs": [],
            },
        )
    if state == "rejected":
        rejection = projection["rejection"]
        if rejection is None or next_action is None:
            return _inconsistent_state(), None
        return (
            _finding(
                "lifecycle-transition-rejected",
                "warning",
                "The transition was rejected by its owning workflow authority.",
                next_action["owner_ref"],
            ),
            {
                "reason_code": rejection["reason_code"],
                "owner_ref": next_action["owner_ref"],
                "required_fix": rejection["reason_detail"],
                "review_at": next_action["review_at"],
                "evidence_refs": [],
            },
        )
    if state == "failed":
        application = projection["application"]
        if (
            application["state"] != "failed"
            or application["failure_code"] is None
            or next_action is None
        ):
            return _inconsistent_state(), None
        return (
            _finding(
                "lifecycle-transition-application-failed",
                "warning",
                "The target application failed and requires its recorded recovery action.",
                next_action["owner_ref"],
            ),
            {
                "reason_code": application["failure_code"],
                "owner_ref": next_action["owner_ref"],
                "required_fix": application["failure_detail"] or "Retry or correct the target application.",
                "review_at": next_action["review_at"],
                "evidence_refs": [application["receipt_ref"]] if application["receipt_ref"] else [],
            },
        )
    if state == "applied":
        application = projection["application"]
        admission = projection["admission"]
        if (
            application["state"] != "applied"
            or application["receipt_ref"] is None
            or application["target_record_ref"] is None
            or admission["state"] != "admitted"
            or admission["receipt_ref"] is None
            or next_action is not None
        ):
            return _inconsistent_state(), None
        return None, None
    if state == "cancelled":
        if projection["cancelled_reason_code"] is None or next_action is not None:
            return _inconsistent_state(), None
        return None, None
    if state == "superseded":
        if projection["superseded_by_transition_id"] is None or next_action is not None:
            return _inconsistent_state(), None
        return None, None
    if state == "rejected":
        return None, None
    return _inconsistent_state(), None


def _gate_escalation(gate: dict[str, Any]) -> dict[str, Any]:
    return {
        "reason_code": gate["gate_id"],
        "owner_ref": gate["owner_ref"],
        "required_fix": gate["required_fix"] or "Resolve the blocking transition gate.",
        "review_at": None,
        "evidence_refs": [gate["evidence_ref"]] if gate["evidence_ref"] else [],
    }


def _inconsistent_state() -> dict[str, str]:
    return _finding(
        "lifecycle-transition-state-inconsistent",
        "blocking",
        "The transition state is missing its required owner context or exact next action.",
        "operator-orchestration-service",
    )


def _evidence_refs(projection: dict[str, Any]) -> list[str]:
    values: list[str] = []
    validation = projection["validation"]
    admission = projection["admission"]
    application = projection["application"]
    for value in (
        validation["receipt_ref"],
        validation["run_ref"],
        admission["receipt_ref"],
        admission["target_record_ref"],
        application["receipt_ref"],
        application["run_ref"],
        application["target_record_ref"],
    ):
        if value:
            values.append(value)
    values.extend(application["resulting_refs"])
    for gate in validation["gates"]:
        if gate["evidence_ref"]:
            values.append(gate["evidence_ref"])
    if projection["blocked_gate"] and projection["blocked_gate"]["evidence_ref"]:
        values.append(projection["blocked_gate"]["evidence_ref"])
    for decision in projection["authority_decisions"]:
        if decision["receipt_ref"]:
            values.append(decision["receipt_ref"])
    for entry in projection["history"]["entries"]:
        values.extend(entry["evidence_refs"])
    return values


def _safe_reference(value: str) -> bool:
    if any(ord(char) < 32 for char in value):
        return False
    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        return False
    query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    return not query_keys.intersection(_SECRET_QUERY_KEYS)


def _finding(code: str, severity: str, message: str, owner_ref: str) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "owner_ref": owner_ref,
    }


def _artifact_digest(record: dict[str, Any], field: str) -> str:
    projection = copy.deepcopy(record)
    projection.pop(field, None)
    return canonical_digest(projection)


def _readiness_uri(digest: str) -> str:
    return f"wgcf://readiness/lifecycle-transition/{digest.removeprefix('sha256:')}"


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LifecycleTransitionRequestError("lifecycle transition timestamp is invalid") from exc
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise LifecycleTransitionUnavailable("lifecycle transition clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_lifecycle_transition_readiness_runtime() -> LifecycleTransitionReadinessService:
    contracts = LifecycleTransitionContractBundle.load()
    if (
        os.environ.get(RUNTIME_PROFILE_ENV) != _ALLOWED_PROFILE
        or os.environ.get("WGCF_LIFECYCLE_TRANSITION_READINESS_ENABLED") != "true"
        or contracts.manifest["runtime_activation"] is not True
    ):
        raise LifecycleTransitionUnavailable(
            "lifecycle transition readiness awaits approved runtime activation",
        )
    return LifecycleTransitionReadinessService(
        session_factory=create_session_factory(),
        service_identity_ref=os.environ.get(SERVICE_IDENTITY_ENV, ""),
        implementation_ref=read_implementation_ref(),
        contract_bundle=contracts,
    )
