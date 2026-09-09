"""Durable, caller-scoped Prototype maturity readiness."""

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
from .db.models import LedgerEvent, PrototypeMaturityReadinessRecord
from .prototype_maturity_contracts import (
    PrototypeMaturityAuthority,
    PrototypeMaturityContracts,
    PrototypeMaturityRequestError,
    PrototypeMaturityUnavailable,
    artifact_digest,
    digest,
    parse_json,
)
from .prototype_maturity_policy import evaluate_prototype_maturity


MAX_PROTOTYPE_MATURITY_EVALUATION_BYTES = 256 * 1024
READINESS_TTL = timedelta(minutes=15)
PROTOTYPE_MATURITY_SERVICE_IDENTITY_ENV = "WGCF_PROTOTYPE_MATURITY_SERVICE_IDENTITY_REF"


class PrototypeMaturityConflict(RuntimeError):
    """An evaluation identity already binds different content or caller."""


class PrototypeMaturityNotFound(LookupError):
    """A caller-scoped Prototype maturity readiness artifact was not found."""


class PrototypeMaturityReadinessService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        authority: PrototypeMaturityAuthority,
        service_identity_ref: str,
        implementation_ref: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not service_identity_ref.strip() or not re.fullmatch(r"[0-9a-f]{40}", implementation_ref):
            raise PrototypeMaturityUnavailable("Prototype maturity evaluator identity is incomplete")
        self.sessions = session_factory
        self.authority = authority
        self.contracts = authority.contracts
        self.identity = service_identity_ref
        self.implementation = implementation_ref
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(self, raw: bytes, *, actor: str) -> dict[str, Any]:
        if len(raw) > MAX_PROTOTYPE_MATURITY_EVALUATION_BYTES:
            raise PrototypeMaturityRequestError("Prototype maturity evaluation exceeds the request limit")
        envelope = parse_json(raw)
        self.contracts.validate("evaluation.schema.json", envelope)
        self.contracts.validate("prototype-maturity-request.schema.json", envelope["request"])
        self.contracts.validate("prototype-maturity-packet.schema.json", envelope["packet"])
        self._validate_digests(envelope)

        try:
            with self.sessions() as session:
                existing = session.get(PrototypeMaturityReadinessRecord, envelope["evaluation_id"])
                if existing is not None:
                    return self._reuse(existing, envelope, actor)

            request = envelope["request"]
            snapshot = self.authority.snapshot(
                request["prototype_id"], self._evidence_refs(request, envelope["packet"])
            )
            evaluated_at = self.clock().astimezone(timezone.utc)
            result = evaluate_prototype_maturity(envelope, snapshot, self.contracts)
            readiness = {
                "schema_version": 1,
                "artifact_type": "prototype-maturity-readiness",
                "readiness_id": request["request_id"].replace(
                    "prototype-maturity-request:", "prototype-maturity-readiness:", 1
                ),
                "evaluated_at": evaluated_at.isoformat().replace("+00:00", "Z"),
                "request_ref": result["request_ref"],
                "packet_ref": result["packet_ref"],
                "prototype_id": request["prototype_id"],
                "transition": request["transition"],
                "observed_state": result["observed_state"],
                "outcome": result["outcome"],
                "checks": result["checks"],
                "findings": result["findings"],
            }
            readiness["readiness_digest"] = digest(readiness)
            try:
                self.contracts.validate("prototype-maturity-readiness.schema.json", readiness)
            except PrototypeMaturityRequestError as exc:
                raise PrototypeMaturityUnavailable(
                    "Prototype maturity evaluator generated an invalid readiness artifact"
                ) from exc

            contract_digest = "sha256:" + self.contracts.manifest["files"]["prototype-maturity.yaml"]
            row = PrototypeMaturityReadinessRecord(
                evaluation_id=envelope["evaluation_id"],
                evaluation_digest=envelope["evaluation_digest"],
                readiness_digest=readiness["readiness_digest"],
                actor=actor,
                authority_revision=snapshot.revision,
                contract_digest=contract_digest,
                implementation_ref=self.implementation,
                policy_version=f"prototype-maturity.v1@{self.contracts.manifest['authority_commit']}",
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
                    existing = session.get(
                        PrototypeMaturityReadinessRecord, envelope["evaluation_id"]
                    )
                    if existing is None:
                        raise PrototypeMaturityUnavailable(
                            "Prototype maturity readiness could not be persisted"
                        )
                    return self._reuse(existing, envelope, actor)
            return self._materialize(row, "created")
        except SQLAlchemyError as exc:
            raise PrototypeMaturityUnavailable(
                "Prototype maturity readiness ledger is unavailable"
            ) from exc

    def read(self, token: str, *, actor: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise PrototypeMaturityRequestError("invalid Prototype maturity readiness token")
        try:
            with self.sessions() as session:
                row = session.scalar(
                    select(PrototypeMaturityReadinessRecord).where(
                        PrototypeMaturityReadinessRecord.readiness_digest == f"sha256:{token}",
                        PrototypeMaturityReadinessRecord.actor == actor,
                    )
                )
                if row is None:
                    raise PrototypeMaturityNotFound("Prototype maturity readiness was not found")
                result = self._materialize(row, "read")
            with self.sessions.begin() as session:
                session.add(self._event(actor, "read", result["readiness"]))
            return result
        except SQLAlchemyError as exc:
            raise PrototypeMaturityUnavailable(
                "Prototype maturity readiness ledger is unavailable"
            ) from exc

    @staticmethod
    def _evidence_refs(request: dict[str, Any], packet: dict[str, Any]) -> list[str]:
        refs = list(request["inputs"]["source_refs"])
        refs.extend(
            ref
            for section in packet["sections"]
            for ref in section["evidence_refs"]
        )
        selected = request["inputs"]["editable_values"].get("selected-evidence-refs", [])
        if isinstance(selected, list):
            refs.extend(selected)
        return sorted(set(refs))

    @staticmethod
    def _validate_digests(envelope: dict[str, Any]) -> None:
        for artifact, field in (
            (envelope["request"], "request_digest"),
            (envelope["packet"], "packet_digest"),
            (envelope, "evaluation_digest"),
        ):
            if artifact_digest(artifact, field) != artifact[field]:
                raise PrototypeMaturityRequestError(f"{field} does not match canonical content")

    def _reuse(
        self,
        row: PrototypeMaturityReadinessRecord,
        envelope: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        if row.evaluation_digest != envelope["evaluation_digest"] or row.actor != actor:
            raise PrototypeMaturityConflict(
                "Prototype maturity evaluation id already binds different content or caller"
            )
        result = self._materialize(row, "reused")
        with self.sessions.begin() as session:
            session.add(self._event(actor, "reused", result["readiness"]))
        return result

    def _materialize(
        self, row: PrototypeMaturityReadinessRecord, resolution: str
    ) -> dict[str, Any]:
        readiness = copy.deepcopy(row.readiness)
        try:
            self.contracts.validate("prototype-maturity-readiness.schema.json", readiness)
        except (PrototypeMaturityRequestError, TypeError, ValueError) as exc:
            raise PrototypeMaturityUnavailable(
                "Prototype maturity readiness schema integrity failed"
            ) from exc
        if (
            artifact_digest(readiness, "readiness_digest") != row.readiness_digest
            or readiness.get("readiness_digest") != row.readiness_digest
        ):
            raise PrototypeMaturityUnavailable("Prototype maturity readiness integrity check failed")
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
        ref = {"uri": _uri(readiness["readiness_digest"]), "digest": readiness["readiness_digest"]}
        return LedgerEvent(
            event_id=f"ledger-event:prototype-maturity:{uuid4().hex}",
            actor=actor,
            action=f"prototype-maturity.readiness.{resolution}",
            target=ref["uri"],
            outcome=readiness["outcome"],
            receipt_refs=[ref],
        )


def _uri(readiness_digest: str) -> str:
    return "wgcf://readiness/prototype-maturity/" + readiness_digest.removeprefix("sha256:")


def build_prototype_maturity_readiness_runtime() -> PrototypeMaturityReadinessService:
    contracts = PrototypeMaturityContracts.load()
    if (
        os.environ.get(RUNTIME_PROFILE_ENV) != "dev-integration"
        or os.environ.get("WGCF_PROTOTYPE_MATURITY_READINESS_ENABLED") != "true"
        or contracts.manifest["runtime_activation"] is not True
    ):
        raise PrototypeMaturityUnavailable(
            "Prototype maturity readiness awaits approved runtime activation"
        )
    root = os.environ.get("WGCF_PROTOTYPE_STUDIO_REPO_ROOT", "").strip()
    if not root:
        raise PrototypeMaturityUnavailable("Prototype Studio source authority is not configured")
    return PrototypeMaturityReadinessService(
        session_factory=create_session_factory(),
        authority=PrototypeMaturityAuthority(Path(root), contracts),
        service_identity_ref=os.environ.get(
            PROTOTYPE_MATURITY_SERVICE_IDENTITY_ENV,
            os.environ.get(SERVICE_IDENTITY_ENV, ""),
        ),
        implementation_ref=read_implementation_ref(),
    )
