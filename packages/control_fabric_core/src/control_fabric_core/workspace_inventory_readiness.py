"""Durable, caller-scoped Workspace active-inventory readiness evaluation."""

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
from .db.models import LedgerEvent, WorkspaceInventoryEvaluationRecord
from .workspace_inventory_contracts import (
    InventoryAuthority,
    InventoryContracts,
    InventoryRequestError,
    InventoryUnavailable,
    artifact_digest,
    canonical_bytes,
    digest,
    parse_json,
)
from .workspace_inventory_policy import evaluate_inventory_promotion


MAX_INVENTORY_REQUEST_BYTES = 64 * 1024


class InventoryConflict(RuntimeError):
    """An evaluation identity already binds different content or caller."""


class InventoryNotFound(LookupError):
    """A caller-scoped readiness artifact was not found."""


class WorkspaceInventoryReadinessService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        authority: InventoryAuthority,
        service_identity_ref: str,
        implementation_ref: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not service_identity_ref.strip() or not re.fullmatch(r"[0-9a-f]{40}", implementation_ref):
            raise InventoryUnavailable("active inventory evaluator identity is incomplete")
        self.sessions = session_factory
        self.authority = authority
        self.contracts = authority.contracts
        self.identity = service_identity_ref
        self.implementation = implementation_ref
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(self, raw: bytes, *, actor: str) -> dict[str, Any]:
        if len(raw) > MAX_INVENTORY_REQUEST_BYTES:
            raise InventoryRequestError("active inventory evaluation exceeds the request limit")
        request = parse_json(raw)
        self.contracts.validate("evaluation.schema.json", request)
        self.contracts.validate(
            "workspace-inventory-promotion-request.schema.json",
            request["request"],
        )
        if artifact_digest(request["request"], "request_digest") != request["request"]["request_digest"]:
            raise InventoryRequestError("promotion request digest does not match canonical content")
        if artifact_digest(request, "evaluation_digest") != request["evaluation_digest"]:
            raise InventoryRequestError("evaluation digest does not match canonical content")

        try:
            with self.sessions() as session:
                row = session.get(WorkspaceInventoryEvaluationRecord, request["evaluation_id"])
                if row is not None:
                    return self._reuse(row, request, actor)

            snapshot = self.authority.snapshot()
            evaluated_at = self.clock().astimezone(timezone.utc)
            result = evaluate_inventory_promotion(
                request,
                snapshot,
                self.contracts,
                evaluated_at,
            )
            readiness = {
                "schema_version": 1,
                "artifact_type": "workspace-inventory-promotion-readiness",
                "readiness_id": f"workspace-inventory-readiness:{request['evaluation_id']}",
                "evaluated_at": evaluated_at.isoformat().replace("+00:00", "Z"),
                "request_ref": {
                    "id": request["request"]["request_id"],
                    "digest": request["request"]["request_digest"],
                },
                "target": copy.deepcopy(request["request"]["target"]),
                "observed_state": result["observed_state"],
                "policy_ref": {
                    "id": f"workspace-active-inventory@{snapshot.revision}",
                    "digest": "sha256:"
                    + self.contracts.manifest["files"][
                        "contracts/workspace-active-inventory.yaml"
                    ],
                },
                "outcome": result["outcome"],
                "findings": result["findings"],
            }
            readiness["readiness_digest"] = digest(readiness)
            try:
                self.contracts.validate(
                    "workspace-inventory-promotion-readiness.schema.json",
                    readiness,
                )
            except InventoryRequestError as exc:
                raise InventoryUnavailable(
                    "active inventory evaluator generated an invalid readiness artifact"
                ) from exc

            row = WorkspaceInventoryEvaluationRecord(
                evaluation_id=request["evaluation_id"],
                evaluation_digest=request["evaluation_digest"],
                readiness_digest=readiness["readiness_digest"],
                actor=actor,
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
                        WorkspaceInventoryEvaluationRecord,
                        request["evaluation_id"],
                    )
                    if existing is None:
                        raise InventoryUnavailable(
                            "active inventory readiness could not be persisted"
                        )
                    return self._reuse(existing, request, actor)
            return self._materialize(row, "created")
        except SQLAlchemyError as exc:
            raise InventoryUnavailable("active inventory readiness ledger is unavailable") from exc

    def read(self, token: str, *, actor: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise InventoryRequestError("invalid active inventory readiness token")
        try:
            with self.sessions() as session:
                row = session.scalar(
                    select(WorkspaceInventoryEvaluationRecord).where(
                        WorkspaceInventoryEvaluationRecord.readiness_digest == f"sha256:{token}",
                        WorkspaceInventoryEvaluationRecord.actor == actor,
                    )
                )
                if row is None:
                    raise InventoryNotFound("active inventory readiness was not found")
                result = self._materialize(row, "read")
            with self.sessions.begin() as session:
                session.add(self._event(actor, "read", result["readiness"]))
            return result
        except SQLAlchemyError as exc:
            raise InventoryUnavailable("active inventory readiness ledger is unavailable") from exc

    def _reuse(
        self,
        row: WorkspaceInventoryEvaluationRecord,
        request: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        if row.evaluation_digest != request["evaluation_digest"] or row.actor != actor:
            raise InventoryConflict("evaluation id already binds different content or caller")
        result = self._materialize(row, "reused")
        with self.sessions.begin() as session:
            session.add(self._event(actor, "reused", result["readiness"]))
        return result

    def _materialize(
        self,
        row: WorkspaceInventoryEvaluationRecord,
        resolution: str,
    ) -> dict[str, Any]:
        readiness = copy.deepcopy(row.readiness)
        try:
            self.contracts.validate(
                "workspace-inventory-promotion-readiness.schema.json",
                readiness,
            )
        except (InventoryRequestError, TypeError, ValueError) as exc:
            raise InventoryUnavailable("active inventory readiness schema integrity failed") from exc
        if (
            artifact_digest(readiness, "readiness_digest") != row.readiness_digest
            or readiness.get("readiness_digest") != row.readiness_digest
            or readiness.get("readiness_id")
            != f"workspace-inventory-readiness:{row.evaluation_id}"
        ):
            raise InventoryUnavailable("active inventory readiness integrity check failed")
        return {
            "readiness": readiness,
            "ledger": {
                "resolution": resolution,
                "state": "durable",
                "ref": {
                    "uri": _uri(row.readiness_digest),
                    "digest": row.readiness_digest,
                },
            },
        }

    @staticmethod
    def _event(actor: str, resolution: str, readiness: dict[str, Any]) -> LedgerEvent:
        return LedgerEvent(
            event_id=f"ledger-event:workspace-inventory:{uuid4().hex}",
            actor=actor,
            action=f"workspace-inventory.readiness.{resolution}",
            target=_uri(readiness["readiness_digest"]),
            outcome=readiness["outcome"],
            receipt_refs=[
                {
                    "uri": _uri(readiness["readiness_digest"]),
                    "digest": readiness["readiness_digest"],
                }
            ],
        )


def _uri(readiness_digest: str) -> str:
    return (
        "wgcf://readiness/workspace-inventory/"
        + readiness_digest.removeprefix("sha256:")
    )


def build_workspace_inventory_readiness_runtime() -> WorkspaceInventoryReadinessService:
    contracts = InventoryContracts.load()
    if (
        os.environ.get(RUNTIME_PROFILE_ENV) != "dev-integration"
        or os.environ.get("WGCF_WORKSPACE_INVENTORY_READINESS_ENABLED") != "true"
        or contracts.manifest["runtime_activation"] is not True
    ):
        raise InventoryUnavailable(
            "workspace active inventory readiness awaits approved runtime activation"
        )
    root = os.environ.get("WGCF_WORKSPACE_GOVERNANCE_REPO_ROOT", "").strip()
    if not root:
        raise InventoryUnavailable("workspace active inventory authority is not configured")
    return WorkspaceInventoryReadinessService(
        session_factory=create_session_factory(),
        authority=InventoryAuthority(Path(root), contracts),
        service_identity_ref=os.environ.get(SERVICE_IDENTITY_ENV, ""),
        implementation_ref=read_implementation_ref(),
    )
