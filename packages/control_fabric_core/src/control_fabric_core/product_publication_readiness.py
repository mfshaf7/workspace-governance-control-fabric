"""Durable, caller-scoped Product Publication readiness evaluation."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .artifact_registry import RUNTIME_PROFILE_ENV, SERVICE_IDENTITY_ENV, read_implementation_ref
from .database import create_session_factory
from .db.models import LedgerEvent, ProductPublicationReadinessRecord
from .product_publication_contracts import (
    ProductPublicationAuthority,
    ProductPublicationContracts,
    ProductPublicationRequestError,
    ProductPublicationUnavailable,
    artifact_digest,
    digest,
    owner_roots_from_json,
    parse_json,
)
from .product_publication_policy import evaluate_product_publication


MAX_PRODUCT_PUBLICATION_EVALUATION_BYTES = 256 * 1024
READINESS_TTL = timedelta(minutes=15)
PRODUCT_PUBLICATION_SERVICE_IDENTITY_ENV = "WGCF_PRODUCT_PUBLICATION_SERVICE_IDENTITY_REF"


class ProductPublicationConflict(RuntimeError):
    """An evaluation, request, or idempotency identity binds different content."""


class ProductPublicationNotFound(LookupError):
    """A caller-scoped Product Publication readiness artifact was not found."""


class ProductPublicationReadinessService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        authority: ProductPublicationAuthority,
        service_identity_ref: str,
        implementation_ref: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not service_identity_ref.strip() or not re.fullmatch(r"[0-9a-f]{40}", implementation_ref):
            raise ProductPublicationUnavailable("Product Publication evaluator identity is incomplete")
        self.sessions = session_factory
        self.authority = authority
        self.contracts = authority.contracts
        self.identity = service_identity_ref
        self.implementation = implementation_ref
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(self, raw: bytes, *, actor: str) -> dict[str, Any]:
        if len(raw) > MAX_PRODUCT_PUBLICATION_EVALUATION_BYTES:
            raise ProductPublicationRequestError("Product Publication evaluation exceeds the request limit")
        envelope = parse_json(raw)
        self.contracts.validate("evaluation.schema.json", envelope)
        for artifact, field in (
            (envelope["request"], "request_digest"),
            (envelope, "evaluation_digest"),
        ):
            if artifact_digest(artifact, field) != artifact[field]:
                raise ProductPublicationRequestError(f"{field} does not match canonical content")

        request = envelope["request"]
        try:
            with self.sessions() as session:
                row = session.get(ProductPublicationReadinessRecord, envelope["evaluation_id"])
                if row is not None:
                    return self._reuse(row, envelope, actor, "reused")
                replay = session.scalar(
                    select(ProductPublicationReadinessRecord).where(
                        ProductPublicationReadinessRecord.actor == actor,
                        or_(
                            ProductPublicationReadinessRecord.idempotency_key == request["idempotency_key"],
                            ProductPublicationReadinessRecord.request_id == request["request_id"],
                        ),
                    )
                )
                if replay is not None:
                    return self._reuse(replay, envelope, actor, "replayed")

            snapshot = self.authority.snapshot(envelope["manifest_source"])
            evaluated_at = self.clock().astimezone(timezone.utc)
            expires_at = evaluated_at + READINESS_TTL
            result = evaluate_product_publication(envelope, snapshot, evaluated_at)
            manifest = snapshot.manifest
            readiness = {
                "schema_version": envelope["schema_version"],
                "artifact_type": "product-publication-readiness",
                "readiness_id": envelope["evaluation_id"].replace(
                    "product-publication-evaluation:", "product-publication-readiness:", 1
                ),
                "evaluated_at": _time(evaluated_at),
                "expires_at": _time(expires_at),
                "request_ref": {
                    "id": request["request_id"],
                    "digest": request["request_digest"],
                    "idempotency_key": request["idempotency_key"],
                },
                "product_id": manifest["product_id"],
                "manifest_binding": {
                    "manifest_id": manifest["manifest_id"],
                    "manifest_digest": snapshot.manifest_digest,
                    "owner_repo": manifest["owner_repo"],
                    "owner_revision": manifest["owner_revision"],
                    "manifest_revision": snapshot.manifest_revision,
                    "path": snapshot.manifest_path,
                },
                "authority_binding": {
                    "revision": snapshot.authority_revision,
                    "contract_digest": snapshot.contract_digest,
                    "products_digest": snapshot.products_digest,
                },
                "target_publication": copy.deepcopy(request["target_publication"]),
                "outcome": result["outcome"],
                "findings": result["findings"],
                "secret_values_embedded": False,
            }
            if envelope["schema_version"] == 2:
                readiness["action"] = request["action"]
                readiness["current_publication_posture"] = request[
                    "current_publication_posture"
                ]
                readiness["previous_receipt"] = copy.deepcopy(request["previous_receipt"])
                readiness["inventory_lifecycle_binding"] = copy.deepcopy(
                    manifest["inventory_lifecycle_binding"]
                )
                readiness["authority_binding"]["history_digest"] = snapshot.history_digest
            readiness["readiness_digest"] = digest(readiness)
            try:
                self.contracts.validate("readiness.schema.json", readiness)
            except ProductPublicationRequestError as exc:
                raise ProductPublicationUnavailable(
                    "Product Publication evaluator generated an invalid readiness artifact"
                ) from exc

            row = ProductPublicationReadinessRecord(
                evaluation_id=envelope["evaluation_id"],
                evaluation_digest=envelope["evaluation_digest"],
                readiness_digest=readiness["readiness_digest"],
                actor=actor,
                request_id=request["request_id"],
                idempotency_key=request["idempotency_key"],
                request_digest=request["request_digest"],
                authority_revision=snapshot.authority_revision,
                contract_digest=snapshot.contract_digest,
                implementation_ref=self.implementation,
                expires_at=expires_at,
                readiness=readiness,
            )
            try:
                with self.sessions.begin() as session:
                    session.add(row)
                    session.add(self._event(actor, "created", readiness))
                    session.flush()
            except IntegrityError:
                with self.sessions() as session:
                    existing = session.scalar(
                        select(ProductPublicationReadinessRecord).where(
                            ProductPublicationReadinessRecord.actor == actor,
                            or_(
                                ProductPublicationReadinessRecord.evaluation_id == envelope["evaluation_id"],
                                ProductPublicationReadinessRecord.idempotency_key == request["idempotency_key"],
                                ProductPublicationReadinessRecord.request_id == request["request_id"],
                            ),
                        )
                    )
                    if existing is None:
                        raise ProductPublicationUnavailable("Product Publication readiness could not be persisted")
                    return self._reuse(existing, envelope, actor, "replayed")
            return self._materialize(row, "created")
        except SQLAlchemyError as exc:
            raise ProductPublicationUnavailable("Product Publication readiness ledger is unavailable") from exc

    def read(self, token: str, *, actor: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise ProductPublicationRequestError("invalid Product Publication readiness token")
        try:
            with self.sessions() as session:
                row = session.scalar(
                    select(ProductPublicationReadinessRecord).where(
                        ProductPublicationReadinessRecord.readiness_digest == f"sha256:{token}",
                        ProductPublicationReadinessRecord.actor == actor,
                    )
                )
                if row is None:
                    raise ProductPublicationNotFound("Product Publication readiness was not found")
                result = self._materialize(row, "read")
            with self.sessions.begin() as session:
                session.add(self._event(actor, "read", result["readiness"]))
            return result
        except SQLAlchemyError as exc:
            raise ProductPublicationUnavailable("Product Publication readiness ledger is unavailable") from exc

    def _reuse(
        self,
        row: ProductPublicationReadinessRecord,
        envelope: dict[str, Any],
        actor: str,
        resolution: str,
    ) -> dict[str, Any]:
        request = envelope["request"]
        if (
            row.actor != actor
            or row.request_digest != request["request_digest"]
            or row.evaluation_digest != envelope["evaluation_digest"]
        ):
            raise ProductPublicationConflict(
                "Product Publication identity already binds different content or caller"
            )
        result = self._materialize(row, resolution)
        with self.sessions.begin() as session:
            session.add(self._event(actor, resolution, result["readiness"]))
        return result

    def _materialize(self, row: ProductPublicationReadinessRecord, resolution: str) -> dict[str, Any]:
        readiness = copy.deepcopy(row.readiness)
        try:
            self.contracts.validate("readiness.schema.json", readiness)
        except ProductPublicationRequestError as exc:
            raise ProductPublicationUnavailable("Product Publication readiness schema integrity failed") from exc
        if artifact_digest(readiness, "readiness_digest") != row.readiness_digest or readiness.get("readiness_digest") != row.readiness_digest:
            raise ProductPublicationUnavailable("Product Publication readiness integrity check failed")
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return {
            "readiness": readiness,
            "ledger": {
                "resolution": resolution,
                "state": "expired" if self.clock().astimezone(timezone.utc) >= expires_at else "durable",
                "ref": {"uri": _uri(row.readiness_digest), "digest": row.readiness_digest},
                "authority_revision": row.authority_revision,
                "contract_digest": row.contract_digest,
                "implementation_ref": row.implementation_ref,
                "service_identity_ref": self.identity,
                "expires_at": _time(expires_at),
            },
        }

    @staticmethod
    def _event(actor: str, resolution: str, readiness: dict[str, Any]) -> LedgerEvent:
        ref = {"uri": _uri(readiness["readiness_digest"]), "digest": readiness["readiness_digest"]}
        return LedgerEvent(
            event_id=f"ledger-event:product-publication:{uuid4().hex}",
            actor=actor,
            action=f"product-publication.readiness.{resolution}",
            target=ref["uri"],
            outcome=readiness["outcome"],
            receipt_refs=[ref],
        )


def _time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _uri(readiness_digest: str) -> str:
    return "wgcf://product-publication/readiness-receipts/" + readiness_digest.removeprefix("sha256:")


def build_product_publication_readiness_runtime() -> ProductPublicationReadinessService:
    contracts = ProductPublicationContracts.load()
    if (
        os.environ.get(RUNTIME_PROFILE_ENV) != "dev-integration"
        or os.environ.get("WGCF_PRODUCT_PUBLICATION_READINESS_ENABLED") != "true"
        or contracts.manifest["runtime_activation"] is not True
    ):
        raise ProductPublicationUnavailable(
            "Product Publication readiness awaits Security review and Platform activation"
        )
    workspace_root = os.environ.get("WGCF_WORKSPACE_GOVERNANCE_REPO_ROOT", "").strip()
    owner_roots = os.environ.get("WGCF_PRODUCT_PUBLICATION_OWNER_REPO_ROOTS", "").strip()
    if not workspace_root or not owner_roots:
        raise ProductPublicationUnavailable("Product Publication authority roots are not configured")
    return ProductPublicationReadinessService(
        session_factory=create_session_factory(),
        authority=ProductPublicationAuthority(
            Path(workspace_root), owner_roots_from_json(owner_roots), contracts
        ),
        service_identity_ref=os.environ.get(
            PRODUCT_PUBLICATION_SERVICE_IDENTITY_ENV,
            os.environ.get(SERVICE_IDENTITY_ENV, ""),
        ),
        implementation_ref=read_implementation_ref(),
    )
