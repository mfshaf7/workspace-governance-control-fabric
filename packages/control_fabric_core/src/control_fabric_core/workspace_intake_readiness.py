"""Authenticated service boundary for immutable Workspace Intake readiness."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
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
from .db.models import LedgerEvent, WorkspaceIntakeEvaluationRecord
from .workspace_intake_contracts import (
    COMMIT_PATTERN, IntakeAuthority, IntakeContracts, IntakeRequestError,
    IntakeUnavailable, artifact_digest, digest, parse_json,
)
from .workspace_intake_policy import evaluate_intake


MAX_INTAKE_REQUEST_BYTES = 65_536


class IntakeConflict(RuntimeError):
    """An evaluation identity is already bound to different content or caller."""


class IntakeNotFound(RuntimeError):
    """No immutable evaluation belongs to this caller and receipt token."""


def prepare_intake_evaluation(raw: bytes, contracts: IntakeContracts) -> dict[str, Any]:
    if len(raw) > MAX_INTAKE_REQUEST_BYTES:
        raise IntakeRequestError("intake evaluation exceeds the payload limit")
    try:
        value = parse_json(raw)
        contracts.validate("evaluation.schema.json", value)
        for name in ("request", "decision"):
            record = value[name]
            contracts.validate(f"workspace-intake-{name}.schema.json", record)
            if artifact_digest(record, f"{name}_digest") != record[f"{name}_digest"]:
                raise IntakeRequestError(f"{name} digest mismatch")
        if artifact_digest(value, "evaluation_digest") != value["evaluation_digest"]:
            raise IntakeRequestError("evaluation digest mismatch")
        for timestamp in (
            value["request"]["requested_at"], value["decision"]["decided_at"],
            value["decision"]["operator_acceptance"]["recorded_at"],
        ):
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return value
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError) as exc:
        raise IntakeRequestError(str(exc)) from exc


class WorkspaceIntakeReadinessService:
    def __init__(
        self, *, session_factory: sessionmaker[Session], authority: IntakeAuthority,
        service_identity_ref: str, implementation_ref: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not service_identity_ref.strip() or not COMMIT_PATTERN.fullmatch(implementation_ref):
            raise IntakeUnavailable("intake evaluator requires service identity and exact implementation revision")
        self.sessions = session_factory
        self.authority = authority
        self.contracts = authority.contracts
        self.identity = service_identity_ref
        self.implementation = implementation_ref
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(self, raw: bytes, *, actor: str) -> dict[str, Any]:
        request = prepare_intake_evaluation(raw, self.contracts)
        if not actor.strip() or len(actor) > 256:
            raise IntakeRequestError("authenticated caller is required")
        try:
            with self.sessions() as session:
                row = session.get(WorkspaceIntakeEvaluationRecord, request["evaluation_id"])
                if row is not None:
                    return self._reuse(row, request, actor)
            snapshot = self.authority.snapshot()
            evaluated_at = self.clock()
            if evaluated_at.tzinfo is None:
                raise IntakeUnavailable("intake evaluator clock must include a timezone")
            result = evaluate_intake(request, snapshot, self.contracts, self.authority.repo_root.parent)
            if any(
                datetime.fromisoformat(value.replace("Z", "+00:00")) > evaluated_at
                for value in (
                    request["decision"]["decided_at"],
                    request["decision"]["operator_acceptance"]["recorded_at"],
                )
            ):
                result["outcome"] = "denied"
                result["next_action"] = "review-decision"
                result["findings"].append({
                    "code": "future-decision", "severity": "blocking",
                    "message": "The decision timestamp is ahead of the evaluation clock.",
                    "next_action": "review-decision",
                })
            receipt = {
                "schema_version": 1,
                "artifact_type": "wgcf-workspace-intake-readiness",
                "evaluation_id": request["evaluation_id"],
                "evaluation_digest": request["evaluation_digest"],
                "session_ref": request["session_ref"],
                "execution_ref": request["execution_ref"],
                "request_ref": {
                    "id": request["request"]["request_id"], "digest": request["request"]["request_digest"],
                },
                "decision_ref": {
                    "id": request["decision"]["decision_id"], "digest": request["decision"]["decision_digest"],
                },
                "target": copy.deepcopy(request["request"]["target"]),
                "authority": {
                    "repo": "workspace-governance", "revision": snapshot.revision,
                    "files": snapshot.file_digests, "bundle_digest": digest(self.contracts.manifest),
                },
                "issuer": {
                    "service_identity_ref": self.identity, "implementation_ref": self.implementation,
                    "caller_id": actor,
                },
                "evaluated_at": evaluated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "canonical_mutation": False,
                **result,
            }
            receipt["receipt_digest"] = digest(receipt)
            try:
                self.contracts.validate("readiness.schema.json", receipt)
            except IntakeRequestError as exc:
                raise IntakeUnavailable("intake evaluator generated an invalid receipt") from exc
            row = WorkspaceIntakeEvaluationRecord(
                evaluation_id=request["evaluation_id"], evaluation_digest=request["evaluation_digest"],
                receipt_digest=receipt["receipt_digest"], actor=actor, receipt=receipt,
            )
            try:
                with self.sessions.begin() as session:
                    session.add(row)
                    session.add(self._event(actor, "created", receipt))
                    session.flush()
            except IntegrityError:
                with self.sessions() as session:
                    existing = session.get(WorkspaceIntakeEvaluationRecord, request["evaluation_id"])
                    if existing is None:
                        raise IntakeUnavailable("intake receipt could not be persisted")
                    return self._reuse(existing, request, actor)
            return self._materialize(row, "created")
        except SQLAlchemyError as exc:
            raise IntakeUnavailable("intake evaluation ledger is unavailable") from exc

    def read(self, token: str, *, actor: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise IntakeRequestError("invalid intake receipt token")
        try:
            with self.sessions() as session:
                row = session.scalar(select(WorkspaceIntakeEvaluationRecord).where(
                    WorkspaceIntakeEvaluationRecord.receipt_digest == f"sha256:{token}",
                    WorkspaceIntakeEvaluationRecord.actor == actor,
                ))
                if row is None:
                    raise IntakeNotFound("intake receipt was not found")
                result = self._materialize(row, "read")
            with self.sessions.begin() as session:
                session.add(self._event(actor, "read", result["receipt"]))
            return result
        except SQLAlchemyError as exc:
            raise IntakeUnavailable("intake evaluation ledger is unavailable") from exc

    def _reuse(self, row: WorkspaceIntakeEvaluationRecord, request: dict[str, Any], actor: str) -> dict[str, Any]:
        if row.evaluation_digest != request["evaluation_digest"] or row.actor != actor:
            raise IntakeConflict("evaluation id already binds different content or caller")
        result = self._materialize(row, "reused")
        with self.sessions.begin() as session:
            session.add(self._event(actor, "reused", result["receipt"]))
        return result

    def _materialize(self, row: WorkspaceIntakeEvaluationRecord, resolution: str) -> dict[str, Any]:
        receipt = copy.deepcopy(row.receipt)
        try:
            self.contracts.validate("readiness.schema.json", receipt)
        except (IntakeRequestError, TypeError, ValueError) as exc:
            raise IntakeUnavailable("intake receipt schema integrity check failed") from exc
        if (
            artifact_digest(receipt, "receipt_digest") != row.receipt_digest
            or receipt.get("receipt_digest") != row.receipt_digest
            or receipt.get("evaluation_digest") != row.evaluation_digest
            or receipt.get("evaluation_id") != row.evaluation_id
            or receipt.get("issuer", {}).get("caller_id") != row.actor
        ):
            raise IntakeUnavailable("intake receipt integrity check failed")
        return {
            "receipt": receipt,
            "ledger": {
                "resolution": resolution, "state": "durable",
                "ref": {
                    "uri": _uri(row.receipt_digest), "digest": row.receipt_digest,
                },
            },
        }

    @staticmethod
    def _event(actor: str, resolution: str, receipt: dict[str, Any]) -> LedgerEvent:
        return LedgerEvent(
            event_id=f"ledger-event:workspace-intake:{uuid4().hex}",
            actor=actor, action=f"workspace-intake.readiness.{resolution}",
            target=_uri(receipt["receipt_digest"]), outcome=receipt["outcome"],
            receipt_refs=[{"uri": _uri(receipt["receipt_digest"]), "digest": receipt["receipt_digest"]}],
        )


def _uri(receipt_digest: str) -> str:
    return f"wgcf://readiness/workspace-intake/{receipt_digest.removeprefix('sha256:')}"


def build_workspace_intake_readiness_runtime() -> WorkspaceIntakeReadinessService:
    contracts = IntakeContracts.load()
    if (
        os.environ.get(RUNTIME_PROFILE_ENV) != "dev-integration"
        or os.environ.get("WGCF_WORKSPACE_INTAKE_READINESS_ENABLED") != "true"
        or contracts.manifest["runtime_activation"] is not True
    ):
        raise IntakeUnavailable("workspace intake readiness awaits approved runtime activation")
    root = os.environ.get("WGCF_WORKSPACE_GOVERNANCE_REPO_ROOT", "").strip()
    if not root:
        raise IntakeUnavailable("workspace intake authority checkout is not configured")
    return WorkspaceIntakeReadinessService(
        session_factory=create_session_factory(),
        authority=IntakeAuthority(Path(root), contracts),
        service_identity_ref=os.environ.get(SERVICE_IDENTITY_ENV, ""),
        implementation_ref=read_implementation_ref(),
    )
