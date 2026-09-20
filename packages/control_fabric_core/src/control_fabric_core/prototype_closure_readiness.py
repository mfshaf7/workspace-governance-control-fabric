"""Durable, caller-scoped Prototype Closure readiness."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .artifact_registry import RUNTIME_PROFILE_ENV, read_implementation_ref
from .database import create_session_factory
from .db.models import LedgerEvent, PrototypeClosureReadinessRecord
from .prototype_closure_authority import (
    PrototypeClosureAuthority,
    PrototypeClosureRequestError,
    PrototypeClosureUnavailable,
    BUNDLE_ROOT,
    load_bundle_manifest,
    studio_digest,
)
from .prototype_closure_evidence import OwnerBackedClosureEvidenceResolver
from .prototype_closure_oos_reader import OosClosureOwnerReader
from .prototype_closure_policy import ClosureEvidenceResolver, evaluate_prototype_closure, resolve_evidence
from .prototype_closure_studio_reader import StudioClosureOwnerReader
from .prototype_maturity_contracts import artifact_digest, digest


MAX_PROTOTYPE_CLOSURE_EVALUATION_BYTES = 128 * 1024
READINESS_TTL = timedelta(minutes=15)


class PrototypeClosureConflict(RuntimeError):
    """One evaluation id binds different content or caller."""


class PrototypeClosureNotFound(LookupError):
    """A caller-scoped readiness artifact was not found."""


def _load_validator(name: str) -> Draft202012Validator:
    try:
        raw = (BUNDLE_ROOT / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != load_bundle_manifest()["transport_schemas"][name]:
            raise PrototypeClosureUnavailable("Prototype Closure transport schema changed")
        schema = json.loads(raw)
        Draft202012Validator.check_schema(schema)
    except (OSError, ValueError, TypeError) as exc:
        raise PrototypeClosureUnavailable("Prototype Closure transport schema is unavailable") from exc
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _validate(validator: Draft202012Validator, value: Any, label: str) -> None:
    errors = list(validator.iter_errors(value))
    if errors:
        raise PrototypeClosureRequestError(f"{label}: {errors[0].message}")


def _uri(readiness_digest: str) -> str:
    return "wgcf://readiness/prototype-closure/" + readiness_digest.removeprefix("sha256:")


class PrototypeClosureReadinessService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        authority: PrototypeClosureAuthority,
        evidence_resolver: ClosureEvidenceResolver,
        service_identity_ref: str,
        implementation_ref: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not service_identity_ref.strip() or not re.fullmatch(r"[0-9a-f]{40}", implementation_ref):
            raise PrototypeClosureUnavailable("Prototype Closure evaluator identity is incomplete")
        self.sessions = session_factory
        self.authority = authority
        self.evidence_resolver = evidence_resolver
        self.identity = service_identity_ref
        self.implementation = implementation_ref
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.evaluation_validator = _load_validator("evaluation.schema.json")
        self.readiness_validator = _load_validator("readiness.schema.json")

    def issue(self, raw: bytes, *, actor: str) -> dict[str, Any]:
        if actor != "operator-orchestration-service":
            raise PrototypeClosureRequestError("Prototype Closure requires the OOS caller")
        if len(raw) > MAX_PROTOTYPE_CLOSURE_EVALUATION_BYTES:
            raise PrototypeClosureRequestError("Prototype Closure evaluation exceeds the request limit")
        try:
            envelope = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise PrototypeClosureRequestError("Prototype Closure evaluation is not JSON") from exc
        _validate(self.evaluation_validator, envelope, "evaluation")
        if artifact_digest(envelope, "evaluation_digest") != envelope["evaluation_digest"]:
            raise PrototypeClosureRequestError("evaluation_digest differs from canonical content")
        request = envelope["request"]
        manifest, policy = self.authority.validate_request(
            self.authority.current_revision(), request
        )
        source = self.authority.snapshot(request["prototype_id"])
        try:
            with self.sessions() as session:
                existing = session.get(PrototypeClosureReadinessRecord, envelope["evaluation_id"])
                if existing is not None:
                    return self._reuse(existing, envelope, actor)
            evidence = resolve_evidence(self.evidence_resolver, request, source)
            result = evaluate_prototype_closure(
                request, source, evidence, policy, envelope["expected_record_digest"]
            )
            evaluated_at = self.clock().astimezone(timezone.utc)
            readiness = {
                "schema_version": 1,
                "artifact_type": "prototype-closure-readiness",
                "readiness_id": envelope["evaluation_id"].replace(
                    "prototype-closure-evaluation:", "prototype-closure-readiness:", 1
                ),
                "evaluated_at": evaluated_at.isoformat().replace("+00:00", "Z"),
                "request_ref": {"id": request["request_id"], "digest": studio_digest(request)},
                "prototype_id": request["prototype_id"],
                "action": request["action"],
                "actor": actor,
                "operator_id": request["operator_id"],
                "source_revision": source.revision,
                "record_digest": source.record_digest,
                "contract_digest": "sha256:" + manifest["files"]["prototype-closure.yaml"],
                "security_review_ref": manifest["security_review"]["merge_commit"],
                **result,
            }
            readiness["readiness_digest"] = digest(readiness)
            try:
                _validate(self.readiness_validator, readiness, "readiness")
            except PrototypeClosureRequestError as exc:
                raise PrototypeClosureUnavailable("evaluator generated invalid readiness") from exc
            row = PrototypeClosureReadinessRecord(
                evaluation_id=envelope["evaluation_id"],
                evaluation_digest=envelope["evaluation_digest"],
                readiness_digest=readiness["readiness_digest"],
                actor=actor,
                authority_revision=source.revision,
                contract_digest=readiness["contract_digest"],
                implementation_ref=self.implementation,
                policy_version=f"prototype-closure.v2@{manifest['authority_commit']}",
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
                    existing = session.get(PrototypeClosureReadinessRecord, envelope["evaluation_id"])
                    if existing is None:
                        raise PrototypeClosureUnavailable("readiness could not be persisted")
                    return self._reuse(existing, envelope, actor)
            return self._materialize(row, "created")
        except SQLAlchemyError as exc:
            raise PrototypeClosureUnavailable("Prototype Closure readiness ledger is unavailable") from exc

    def read(self, token: str, *, actor: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise PrototypeClosureRequestError("invalid Prototype Closure readiness token")
        try:
            with self.sessions() as session:
                row = session.scalar(select(PrototypeClosureReadinessRecord).where(
                    PrototypeClosureReadinessRecord.readiness_digest == f"sha256:{token}",
                    PrototypeClosureReadinessRecord.actor == actor,
                ))
                if row is None:
                    raise PrototypeClosureNotFound("Prototype Closure readiness was not found")
                result = self._materialize(row, "read")
            with self.sessions.begin() as session:
                session.add(self._event(actor, "read", result["readiness"]))
            return result
        except SQLAlchemyError as exc:
            raise PrototypeClosureUnavailable("Prototype Closure readiness ledger is unavailable") from exc

    def _reuse(
        self, row: PrototypeClosureReadinessRecord, envelope: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        if row.evaluation_digest != envelope["evaluation_digest"] or row.actor != actor:
            raise PrototypeClosureConflict("evaluation id binds different content or caller")
        result = self._materialize(row, "reused")
        with self.sessions.begin() as session:
            session.add(self._event(actor, "reused", result["readiness"]))
        return result

    def _materialize(self, row: PrototypeClosureReadinessRecord, resolution: str) -> dict[str, Any]:
        readiness = copy.deepcopy(row.readiness)
        try:
            _validate(self.readiness_validator, readiness, "readiness")
        except PrototypeClosureRequestError as exc:
            raise PrototypeClosureUnavailable("readiness schema integrity failed") from exc
        if (
            readiness.get("readiness_digest") != row.readiness_digest
            or artifact_digest(readiness, "readiness_digest") != row.readiness_digest
            or digest(readiness["evidence"]) != readiness["evidence_digest"]
        ):
            raise PrototypeClosureUnavailable("readiness digest integrity failed")
        now = self.clock().astimezone(timezone.utc)
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        current_revision = self.authority.current_revision()
        state = "stale" if current_revision != row.authority_revision else (
            "expired" if now >= expires_at else "durable"
        )
        return {
            "readiness": readiness,
            "ledger": {
                "resolution": resolution,
                "state": state,
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
            event_id=f"ledger-event:prototype-closure:{uuid4().hex}",
            actor=actor,
            action=f"prototype-closure.readiness.{resolution}",
            target=ref["uri"],
            outcome=readiness["outcome"],
            receipt_refs=[ref],
        )


def build_prototype_closure_readiness_runtime() -> PrototypeClosureReadinessService:
    manifest = load_bundle_manifest()
    if (
        os.environ.get(RUNTIME_PROFILE_ENV) != "dev-integration"
        or os.environ.get("WGCF_PROTOTYPE_CLOSURE_READINESS_ENABLED") != "true"
        or manifest["runtime_activation"] is not True
    ):
        raise PrototypeClosureUnavailable("Prototype Closure awaits approved runtime activation")
    studio_root = os.environ.get("WGCF_PROTOTYPE_STUDIO_REPO_ROOT", "").strip()
    oos_url = os.environ.get("WGCF_PROTOTYPE_CLOSURE_OOS_URL", "").strip()
    credential_file = os.environ.get("WGCF_PROTOTYPE_CLOSURE_OOS_CREDENTIAL_FILE", "").strip()
    identity = os.environ.get("WGCF_PROTOTYPE_CLOSURE_SERVICE_IDENTITY_REF", "").strip()
    if not all((studio_root, oos_url, credential_file, identity)):
        raise PrototypeClosureUnavailable("Prototype Closure owner reader configuration is incomplete")
    authority = PrototypeClosureAuthority(Path(studio_root))
    oos = OosClosureOwnerReader(base_url=oos_url, credential_file=Path(credential_file))
    studio = StudioClosureOwnerReader(authority)
    resolver = OwnerBackedClosureEvidenceResolver({
        "workspace-prototype-studio": studio,
        "operator-orchestration-service": oos,
        "workspace-delivery-art": oos,
        "platform-engineering": oos,
        "requested-owner": oos,
    })
    return PrototypeClosureReadinessService(
        session_factory=create_session_factory(),
        authority=authority,
        evidence_resolver=resolver,
        service_identity_ref=identity,
        implementation_ref=read_implementation_ref(),
    )
