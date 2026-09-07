"""Durable, caller-scoped Prototype Landing readiness."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .artifact_registry import RUNTIME_PROFILE_ENV, SERVICE_IDENTITY_ENV, read_implementation_ref
from .database import create_session_factory
from .db.models import LedgerEvent, PrototypeLandingReadinessRecord
from .prototype_landing_contracts import (
    PrototypeLandingAuthority,
    PrototypeLandingContracts,
    PrototypeLandingRequestError,
    PrototypeLandingUnavailable,
    artifact_digest,
    digest,
    parse_json,
)
from .prototype_landing_policy import evaluate_prototype_landing


MAX_PROTOTYPE_LANDING_EVALUATION_BYTES = 256 * 1024
READINESS_TTL = timedelta(minutes=15)


class PrototypeLandingConflict(RuntimeError):
    """An evaluation identity already binds different content or caller."""


class PrototypeLandingNotFound(LookupError):
    """A caller-scoped Prototype Landing readiness artifact was not found."""


class PrototypeLandingReadinessService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        authority: PrototypeLandingAuthority,
        service_identity_ref: str,
        implementation_ref: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not service_identity_ref.strip() or not re.fullmatch(r"[0-9a-f]{40}", implementation_ref):
            raise PrototypeLandingUnavailable("Prototype Landing evaluator identity is incomplete")
        self.sessions = session_factory
        self.authority = authority
        self.contracts = authority.contracts
        self.identity = service_identity_ref
        self.implementation = implementation_ref
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(self, raw: bytes, *, actor: str) -> dict[str, Any]:
        if len(raw) > MAX_PROTOTYPE_LANDING_EVALUATION_BYTES:
            raise PrototypeLandingRequestError("Prototype Landing evaluation exceeds the request limit")
        envelope = parse_json(raw)
        self.contracts.validate("evaluation.schema.json", envelope)
        self.contracts.validate("prototype-landing-entry-packet.schema.json", envelope["entry_packet"])
        self.contracts.validate("prototype-landing-request.schema.json", envelope["request"])
        self.contracts.validate("prototype-landing-plan.schema.json", envelope["plan"])
        self._validate_digests(envelope)

        try:
            with self.sessions() as session:
                existing = session.get(PrototypeLandingReadinessRecord, envelope["evaluation_id"])
                if existing is not None:
                    return self._reuse(existing, envelope, actor)

            request = envelope["request"]
            snapshot = self.authority.snapshot(
                request["prototype"]["id"], request["source_plan"]["source_ref"]
            )
            evaluated_at = self.clock().astimezone(timezone.utc)
            result = evaluate_prototype_landing(envelope, snapshot)
            readiness = {
                "schema_version": 1,
                "artifact_type": "prototype-landing-readiness",
                "readiness_id": request["request_id"].replace(
                    "prototype-landing-request:", "prototype-landing-readiness:", 1
                ),
                "evaluated_at": evaluated_at.isoformat().replace("+00:00", "Z"),
                "request_ref": result["request_ref"],
                "plan_ref": {
                    "id": envelope["plan"]["plan_id"],
                    "digest": envelope["plan"]["plan_digest"],
                },
                "observed_state": result["observed_state"],
                "outcome": result["outcome"],
                "checks": result["checks"],
                "findings": result["findings"],
                "security_trigger_refs": result["security_trigger_refs"],
            }
            readiness["readiness_digest"] = digest(readiness)
            try:
                self.contracts.validate("prototype-landing-readiness.schema.json", readiness)
            except PrototypeLandingRequestError as exc:
                raise PrototypeLandingUnavailable(
                    "Prototype Landing evaluator generated an invalid readiness artifact"
                ) from exc

            contract_digest = "sha256:" + self.contracts.manifest["files"]["prototype-landing.yaml"]
            row = PrototypeLandingReadinessRecord(
                evaluation_id=envelope["evaluation_id"],
                evaluation_digest=envelope["evaluation_digest"],
                readiness_digest=readiness["readiness_digest"],
                actor=actor,
                authority_revision=snapshot.revision,
                contract_digest=contract_digest,
                implementation_ref=self.implementation,
                policy_version=f"prototype-landing.v1@{self.contracts.manifest['authority_commit']}",
                expires_at=evaluated_at + READINESS_TTL,
                readiness=readiness,
            )
            try:
                with self.sessions.begin() as session:
                    session.add(row)
                    session.add(self._event(actor, "created", readiness))
                    session.flush()
            except IntegrityError:
                with self.sessions() as session:
                    existing = session.get(PrototypeLandingReadinessRecord, envelope["evaluation_id"])
                    if existing is None:
                        raise PrototypeLandingUnavailable(
                            "Prototype Landing readiness could not be persisted"
                        )
                    return self._reuse(existing, envelope, actor)
            return self._materialize(row, "created")
        except SQLAlchemyError as exc:
            raise PrototypeLandingUnavailable(
                "Prototype Landing readiness ledger is unavailable"
            ) from exc

    def read(self, token: str, *, actor: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise PrototypeLandingRequestError("invalid Prototype Landing readiness token")
        try:
            with self.sessions() as session:
                row = session.scalar(
                    select(PrototypeLandingReadinessRecord).where(
                        PrototypeLandingReadinessRecord.readiness_digest == f"sha256:{token}",
                        PrototypeLandingReadinessRecord.actor == actor,
                    )
                )
                if row is None:
                    raise PrototypeLandingNotFound("Prototype Landing readiness was not found")
                result = self._materialize(row, "read")
            with self.sessions.begin() as session:
                session.add(self._event(actor, "read", result["readiness"]))
            return result
        except SQLAlchemyError as exc:
            raise PrototypeLandingUnavailable(
                "Prototype Landing readiness ledger is unavailable"
            ) from exc

    def _validate_digests(self, envelope: dict[str, Any]) -> None:
        artifacts = (
            (envelope["entry_packet"], "packet_digest"),
            (envelope["request"], "request_digest"),
            (envelope["plan"], "plan_digest"),
            (envelope, "evaluation_digest"),
        )
        for artifact, field in artifacts:
            if artifact_digest(artifact, field) != artifact[field]:
                raise PrototypeLandingRequestError(f"{field} does not match canonical content")

    def _reuse(
        self,
        row: PrototypeLandingReadinessRecord,
        envelope: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        if row.evaluation_digest != envelope["evaluation_digest"] or row.actor != actor:
            raise PrototypeLandingConflict(
                "Prototype Landing evaluation id already binds different content or caller"
            )
        result = self._materialize(row, "reused")
        with self.sessions.begin() as session:
            session.add(self._event(actor, "reused", result["readiness"]))
        return result

    def _materialize(
        self, row: PrototypeLandingReadinessRecord, resolution: str
    ) -> dict[str, Any]:
        readiness = copy.deepcopy(row.readiness)
        try:
            self.contracts.validate("prototype-landing-readiness.schema.json", readiness)
        except (PrototypeLandingRequestError, TypeError, ValueError) as exc:
            raise PrototypeLandingUnavailable(
                "Prototype Landing readiness schema integrity failed"
            ) from exc
        if (
            artifact_digest(readiness, "readiness_digest") != row.readiness_digest
            or readiness.get("readiness_digest") != row.readiness_digest
        ):
            raise PrototypeLandingUnavailable("Prototype Landing readiness integrity check failed")
        now = self.clock().astimezone(timezone.utc)
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return {
            "readiness": readiness,
            "ledger": {
                "resolution": resolution,
                "state": "expired" if now >= expires_at else "durable",
                "ref": {"uri": _uri(row.readiness_digest), "digest": row.readiness_digest},
                "authority_revision": row.authority_revision,
                "contract_digest": row.contract_digest,
                "implementation_ref": row.implementation_ref,
                "service_identity_ref": self.identity,
                "policy_version": row.policy_version,
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            },
        }

    @staticmethod
    def _event(actor: str, resolution: str, readiness: dict[str, Any]) -> LedgerEvent:
        return LedgerEvent(
            event_id=f"ledger-event:prototype-landing:{uuid4().hex}",
            actor=actor,
            action=f"prototype-landing.readiness.{resolution}",
            target=_uri(readiness["readiness_digest"]),
            outcome=readiness["outcome"],
            receipt_refs=[{
                "uri": _uri(readiness["readiness_digest"]),
                "digest": readiness["readiness_digest"],
            }],
        )


def _uri(readiness_digest: str) -> str:
    return "wgcf://readiness/prototype-landing/" + readiness_digest.removeprefix("sha256:")


def build_prototype_landing_readiness_runtime() -> PrototypeLandingReadinessService:
    contracts = PrototypeLandingContracts.load()
    if (
        os.environ.get(RUNTIME_PROFILE_ENV) != "dev-integration"
        or os.environ.get("WGCF_PROTOTYPE_LANDING_READINESS_ENABLED") != "true"
        or contracts.manifest["runtime_activation"] is not True
    ):
        raise PrototypeLandingUnavailable(
            "Prototype Landing readiness awaits approved runtime activation"
        )
    root = os.environ.get("WGCF_PROTOTYPE_STUDIO_REPO_ROOT", "").strip()
    if not root:
        raise PrototypeLandingUnavailable("Prototype Studio source authority is not configured")
    return PrototypeLandingReadinessService(
        session_factory=create_session_factory(),
        authority=PrototypeLandingAuthority(Path(root), contracts),
        service_identity_ref=os.environ.get(SERVICE_IDENTITY_ENV, ""),
        implementation_ref=read_implementation_ref(),
    )
