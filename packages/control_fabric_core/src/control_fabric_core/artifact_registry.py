"""Bounded Delivery ART artifact registry and immutable custody receipts."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterator
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .artifact_storage import (
    ArtifactObjectStore,
    ArtifactStorageIntegrityError,
    ArtifactStorageSettings,
    S3ArtifactObjectStore,
    StoredArtifactObject,
    delivery_art_object_key,
)
from .canonical_json import (
    canonical_digest,
    canonical_json_bytes,
    delivery_art_content_projection,
    sha256_digest,
    strict_json_loads,
)
from .database import create_session_factory
from .db.models import (
    DeliveryArtifactCustodyReceipt,
    DeliveryArtifactRegistryEntry,
    LedgerEvent,
)


MAX_ARTIFACT_CONTENT_BYTES = 1_048_576
MAX_REGISTRY_REQUEST_BYTES = MAX_ARTIFACT_CONTENT_BYTES + 8_192
SOURCE_REVISION_PATH = Path("/opt/wgcf/build/source-revision")

RUNTIME_PROFILE_ENV = "WGCF_RUNTIME_PROFILE"
SERVICE_IDENTITY_ENV = "WGCF_EVIDENCE_STORAGE_IDENTITY_REF"
OOS_CALLER_ID_ENV = "WGCF_ARTIFACT_REGISTRY_OOS_CALLER_ID"
OOS_CALLER_SECRET_ENV = "WGCF_ARTIFACT_REGISTRY_OOS_CALLER_SECRET"
RECONCILER_CALLER_ID_ENV = "WGCF_ARTIFACT_REGISTRY_RECONCILER_CALLER_ID"
RECONCILER_CALLER_SECRET_ENV = "WGCF_ARTIFACT_REGISTRY_RECONCILER_CALLER_SECRET"

DEFAULT_OOS_CALLER_ID = "operator-orchestration-service"
DEFAULT_RECONCILER_CALLER_ID = "workspace-governance-control-fabric"

_DELIVERY_ID_PATTERN = re.compile(r"^delivery-[1-9][0-9]*$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMPLEMENTATION_REF_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REGISTRY_URI_PATTERN = re.compile(r"^wgcf://artifacts/delivery-art/sha256/[0-9a-f]{64}$")

_ARTIFACT_ID_CONTRACTS: dict[str, tuple[str, re.Pattern[str]]] = {
    "delivery_art_architecture_packet": (
        "artifact_id",
        re.compile(r"^architecture-packet:[a-z0-9][a-z0-9._:-]*$"),
    ),
    "delivery_art_work_start_record": (
        "artifact_id",
        re.compile(r"^work-start:[a-z0-9][a-z0-9._:-]*$"),
    ),
    "art_review_packet": (
        "packet_id",
        re.compile(r"^review-packet:[a-z0-9][a-z0-9._:-]*$"),
    ),
}


class ArtifactRegistryError(RuntimeError):
    """Base failure for the Delivery ART registry."""


class ArtifactRegistryContractError(ArtifactRegistryError):
    """The caller supplied content outside the approved registry contract."""


class ArtifactRegistryUnauthorized(ArtifactRegistryError):
    """Registry caller authentication failed."""


class ArtifactRegistryForbidden(ArtifactRegistryError):
    """The authenticated caller is not allowed to perform this operation."""


class ArtifactRegistryUnavailable(ArtifactRegistryError):
    """The bounded registry runtime is not configured or available."""


class ArtifactRegistryNotFound(ArtifactRegistryError):
    """The requested content digest is not registered."""


class ArtifactRegistryConflict(ArtifactRegistryError):
    """The requested append-only mutation conflicts with registry history."""


@contextmanager
def _database_error_boundary() -> Iterator[None]:
    """Keep metadata-store failures inside the registry availability contract."""

    try:
        yield
    except ArtifactRegistryError:
        raise
    except SQLAlchemyError as exc:
        raise ArtifactRegistryUnavailable(
            "artifact registry metadata store is unavailable",
        ) from exc


@dataclass(frozen=True)
class PreparedArtifact:
    artifact_content: dict[str, Any]
    artifact_id: str
    artifact_type: str
    canonical_bytes: bytes
    content_digest: str
    delivery_id: str
    supersedes: dict[str, str] | None


@dataclass(frozen=True)
class _ArtifactPersistenceTimeline:
    storage: datetime
    receipt: datetime
    artifact: datetime


@dataclass(frozen=True)
class ArtifactRegistryResult:
    artifact: dict[str, Any]
    custody_receipt: dict[str, Any]
    generation: int
    resolution: str

    def to_record(self) -> dict[str, Any]:
        return {
            "artifact": copy.deepcopy(self.artifact),
            "custody_receipt": copy.deepcopy(self.custody_receipt),
            "registry": {
                "artifact_ref": {
                    "uri": self.artifact["custody"]["uri"],
                    "digest": self.artifact["integrity"]["content_digest"],
                },
                "custody_receipt_ref": copy.deepcopy(
                    self.artifact["custody"]["receipt_ref"],
                ),
                "generation": self.generation,
                "resolution": self.resolution,
                "state": "durable",
            },
        }


@dataclass(frozen=True)
class ArtifactReconciliationResult:
    artifact_ref: dict[str, str]
    custody_receipt_ref: dict[str, str]
    generation: int
    state: str = "consistent"

    def to_record(self) -> dict[str, Any]:
        return {
            "artifact_ref": dict(self.artifact_ref),
            "custody_receipt_ref": dict(self.custody_receipt_ref),
            "generation": self.generation,
            "state": self.state,
        }


@dataclass(frozen=True)
class _RegistrySnapshot:
    artifact_id: str
    artifact_type: str
    content_digest: str
    delivery_id: str
    generation: int
    object_key: str
    object_version_id: str
    artifact_persisted_at: str
    receipt_persisted_at: str
    receipt: dict[str, Any]
    receipt_digest: str
    receipt_id: str
    receipt_uri: str
    registry_uri: str
    storage_receipt_ref: str
    supersedes_content_digest: str | None
    supersedes_receipt_ref: dict[str, str] | None
    supersedes_registry_uri: str | None


class ArtifactRegistryAuthorizer:
    """Fail-closed method-scoped authentication for registry callers."""

    def __init__(
        self,
        *,
        oos_secret: str,
        reconciler_secret: str,
        oos_caller_id: str = DEFAULT_OOS_CALLER_ID,
        reconciler_caller_id: str = DEFAULT_RECONCILER_CALLER_ID,
    ) -> None:
        if len(oos_secret) < 32 or len(reconciler_secret) < 32:
            raise ArtifactRegistryUnavailable("registry caller secrets must contain at least 32 characters")
        if not oos_caller_id or not reconciler_caller_id or oos_caller_id == reconciler_caller_id:
            raise ArtifactRegistryUnavailable("registry caller identities must be distinct")
        self._callers = {
            oos_caller_id: (
                oos_secret,
                frozenset({"register", "read", "evaluate-readiness", "read-readiness"}),
            ),
            reconciler_caller_id: (
                reconciler_secret,
                frozenset({"read", "reconcile", "read-readiness"}),
            ),
        }

    @classmethod
    def from_environment(cls) -> ArtifactRegistryAuthorizer:
        oos_secret = os.environ.get(OOS_CALLER_SECRET_ENV, "")
        reconciler_secret = os.environ.get(RECONCILER_CALLER_SECRET_ENV, "")
        if not oos_secret or not reconciler_secret:
            raise ArtifactRegistryUnavailable("registry caller authentication is not configured")
        return cls(
            oos_caller_id=os.environ.get(OOS_CALLER_ID_ENV, DEFAULT_OOS_CALLER_ID).strip(),
            oos_secret=oos_secret,
            reconciler_caller_id=os.environ.get(
                RECONCILER_CALLER_ID_ENV,
                DEFAULT_RECONCILER_CALLER_ID,
            ).strip(),
            reconciler_secret=reconciler_secret,
        )

    def authorize(self, caller_id: str, caller_secret: str, operation: str) -> None:
        configured = self._callers.get(caller_id)
        if configured is None or not caller_secret:
            raise ArtifactRegistryUnauthorized("registry caller authentication failed")
        expected_secret, operations = configured
        if not hmac.compare_digest(caller_secret, expected_secret):
            raise ArtifactRegistryUnauthorized("registry caller authentication failed")
        if operation not in operations:
            raise ArtifactRegistryForbidden("registry caller is not authorized for this operation")


def prepare_artifact_registration(raw_request: bytes) -> PreparedArtifact:
    """Parse and verify one bounded artifact registration envelope."""

    try:
        return _prepare_artifact_registration(raw_request)
    except ArtifactRegistryContractError:
        raise
    except RecursionError as exc:
        raise ArtifactRegistryContractError(
            "artifact JSON nesting exceeds the supported depth",
        ) from exc
    except ValueError as exc:
        raise ArtifactRegistryContractError(str(exc)) from exc


def _prepare_artifact_registration(raw_request: bytes) -> PreparedArtifact:
    """Implement registration parsing behind the bounded contract boundary."""

    if len(raw_request) > MAX_REGISTRY_REQUEST_BYTES:
        raise ArtifactRegistryContractError("registry request exceeds the bounded payload limit")
    try:
        request = strict_json_loads(raw_request)
    except ValueError as exc:
        raise ArtifactRegistryContractError(str(exc)) from exc
    if not isinstance(request, dict):
        raise ArtifactRegistryContractError("registry request must be a JSON object")
    if set(request) != {"artifact_content", "content_digest"}:
        raise ArtifactRegistryContractError(
            "registry request must contain only artifact_content and content_digest",
        )
    artifact_content = request.get("artifact_content")
    content_digest = request.get("content_digest")
    if not isinstance(artifact_content, dict):
        raise ArtifactRegistryContractError("artifact_content must be an object")
    if not isinstance(content_digest, str) or not _DIGEST_PATTERN.fullmatch(content_digest):
        raise ArtifactRegistryContractError("content_digest must be a lowercase sha256 digest")

    canonical_bytes = canonical_json_bytes(artifact_content)
    if len(canonical_bytes) > MAX_ARTIFACT_CONTENT_BYTES:
        raise ArtifactRegistryContractError("artifact content exceeds the bounded payload limit")
    if sha256_digest(canonical_bytes) != content_digest:
        raise ArtifactRegistryContractError("content_digest does not match canonical artifact content")

    artifact_type = artifact_content.get("artifact_type")
    identity_contract = _ARTIFACT_ID_CONTRACTS.get(artifact_type)
    if identity_contract is None:
        raise ArtifactRegistryContractError("artifact_type is not approved for Delivery ART custody")
    identity_field, identity_pattern = identity_contract
    artifact_id = artifact_content.get(identity_field)
    if not isinstance(artifact_id, str) or not identity_pattern.fullmatch(artifact_id):
        raise ArtifactRegistryContractError(f"{identity_field} does not match its artifact contract")
    delivery_id = artifact_content.get("delivery_id")
    if not isinstance(delivery_id, str) or not _DELIVERY_ID_PATTERN.fullmatch(delivery_id):
        raise ArtifactRegistryContractError("delivery_id must use delivery-<positive integer>")

    integrity = artifact_content.get("integrity")
    if integrity != {"canonicalization": "RFC8785", "algorithm": "sha256"}:
        raise ArtifactRegistryContractError(
            "artifact_content.integrity must contain only RFC8785 canonicalization and sha256 algorithm",
        )
    supersedes = _validate_content_custody(artifact_content.get("custody"))
    if delivery_art_content_projection(artifact_content) != artifact_content:
        raise ArtifactRegistryContractError(
            "artifact_content contains generated digest or custody fields",
        )
    return PreparedArtifact(
        artifact_content=copy.deepcopy(artifact_content),
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        canonical_bytes=canonical_bytes,
        content_digest=content_digest,
        delivery_id=delivery_id,
        supersedes=supersedes,
    )


def _validate_content_custody(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"supersedes"}:
        raise ArtifactRegistryContractError(
            "artifact_content.custody may contain only a non-null supersedes reference",
        )
    supersedes = value.get("supersedes")
    if not isinstance(supersedes, dict) or set(supersedes) != {"uri", "digest"}:
        raise ArtifactRegistryContractError("custody.supersedes must contain uri and digest")
    uri = supersedes.get("uri")
    digest = supersedes.get("digest")
    if not isinstance(uri, str) or not _REGISTRY_URI_PATTERN.fullmatch(uri):
        raise ArtifactRegistryContractError("custody.supersedes.uri is not a WGCF artifact URI")
    if not isinstance(digest, str) or not _DIGEST_PATTERN.fullmatch(digest):
        raise ArtifactRegistryContractError("custody.supersedes.digest is not a sha256 digest")
    return {"uri": uri, "digest": digest}


class DeliveryArtifactRegistry:
    """Persist and resolve approved Delivery ART artifacts without authorship."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        storage: ArtifactObjectStore,
        service_identity_ref: str,
        implementation_ref: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not service_identity_ref.strip():
            raise ArtifactRegistryUnavailable("registry service identity is not configured")
        if not _IMPLEMENTATION_REF_PATTERN.fullmatch(implementation_ref):
            raise ArtifactRegistryUnavailable("registry implementation_ref must be an exact Git commit")
        self._session_factory = session_factory
        self._storage = storage
        self._service_identity_ref = service_identity_ref.strip()
        self._implementation_ref = implementation_ref
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def register(self, raw_request: bytes, *, actor: str) -> ArtifactRegistryResult:
        with _database_error_boundary():
            return self._register(raw_request, actor=actor)

    def _register(self, raw_request: bytes, *, actor: str) -> ArtifactRegistryResult:
        prepared = prepare_artifact_registration(raw_request)
        existing = self._snapshot_by_digest(prepared.content_digest)
        if existing is not None:
            self._assert_same_registration(existing, prepared)
            result = self._materialize(existing, resolution="reused")
            self._append_ledger_event(actor, "artifact.registry.reused", result)
            return result

        head = self._subject_head(prepared)
        self._validate_supersession(prepared, head)
        binding = self._storage.ensure_content(prepared.content_digest, prepared.canonical_bytes)
        self._validate_storage_binding(binding, prepared)
        timeline = self._persistence_timeline(head)

        try:
            inserted = self._insert_registry_state(
                actor=actor,
                binding=binding,
                prepared=prepared,
                timeline=timeline,
            )
        except IntegrityError as exc:
            existing = self._snapshot_by_digest(prepared.content_digest)
            if existing is None:
                raise ArtifactRegistryConflict(
                    "artifact subject advanced concurrently; supersede the latest durable artifact",
                ) from exc
            self._assert_same_registration(existing, prepared)
            result = self._materialize(existing, resolution="reused")
            self._append_ledger_event(actor, "artifact.registry.reused", result)
            return result

        if not inserted:
            existing = self._snapshot_by_digest(prepared.content_digest)
            if existing is None:
                raise ArtifactRegistryConflict("registry idempotency state could not be resolved")
            self._assert_same_registration(existing, prepared)
            result = self._materialize(existing, resolution="reused")
            self._append_ledger_event(actor, "artifact.registry.reused", result)
            return result

        snapshot = self._snapshot_by_digest(prepared.content_digest)
        if snapshot is None:
            raise ArtifactRegistryUnavailable("persisted registry entry could not be resolved")
        return self._materialize(snapshot, resolution="created")

    def read(self, content_digest: str, *, actor: str) -> ArtifactRegistryResult:
        with _database_error_boundary():
            snapshot = self._required_snapshot(content_digest)
            result = self._materialize(snapshot, resolution="read")
            self._append_ledger_event(actor, "artifact.registry.read", result)
            return result

    def reconcile(self, content_digest: str, *, actor: str) -> ArtifactReconciliationResult:
        with _database_error_boundary():
            snapshot = self._required_snapshot(content_digest)
            result = self._materialize(snapshot, resolution="reconciled")
            self._append_ledger_event(actor, "artifact.registry.reconciled", result)
            return ArtifactReconciliationResult(
                artifact_ref=copy.deepcopy(result.to_record()["registry"]["artifact_ref"]),
                custody_receipt_ref=copy.deepcopy(
                    result.to_record()["registry"]["custody_receipt_ref"],
                ),
                generation=result.generation,
            )

    def _insert_registry_state(
        self,
        *,
        actor: str,
        binding: StoredArtifactObject,
        prepared: PreparedArtifact,
        timeline: _ArtifactPersistenceTimeline,
    ) -> bool:
        with self._session_factory.begin() as session:
            existing = self._entry_by_digest(session, prepared.content_digest)
            if existing is not None:
                return False
            head = self._subject_head_in_session(session, prepared)
            self._validate_supersession(prepared, head)
            generation = 1 if head is None else head.generation + 1
            prior_receipt = None
            if head is not None:
                prior_receipt = session.scalar(
                    select(DeliveryArtifactCustodyReceipt).where(
                        DeliveryArtifactCustodyReceipt.registry_uri == head.registry_uri,
                    ),
                )
                if prior_receipt is None:
                    raise ArtifactRegistryUnavailable("superseded artifact has no custody receipt")

            registry_uri = _registry_uri(prepared.content_digest)
            storage_receipt_ref = self._storage_receipt_ref(binding, prepared.content_digest)
            receipt = self._build_custody_receipt(
                prepared=prepared,
                prior_receipt=prior_receipt,
                registry_uri=registry_uri,
                receipt_persisted_at=timeline.receipt,
                storage_persisted_at=timeline.storage,
                storage_receipt_ref=storage_receipt_ref,
            )
            entry = DeliveryArtifactRegistryEntry(
                artifact_id=prepared.artifact_id,
                artifact_type=prepared.artifact_type,
                content_digest=prepared.content_digest,
                delivery_id=prepared.delivery_id,
                generation=generation,
                object_key=binding.object_key,
                object_version_id=binding.version_id,
                persisted_at=timeline.artifact,
                registry_uri=registry_uri,
                storage_receipt_ref=storage_receipt_ref,
                supersedes_content_digest=(
                    prepared.supersedes["digest"] if prepared.supersedes else None
                ),
                supersedes_registry_uri=(
                    prepared.supersedes["uri"] if prepared.supersedes else None
                ),
            )
            custody_receipt = DeliveryArtifactCustodyReceipt(
                persisted_at=timeline.receipt,
                receipt=receipt,
                receipt_digest=receipt["integrity"]["content_digest"],
                receipt_id=receipt["receipt_id"],
                receipt_uri=receipt["custody"]["uri"],
                registry_uri=registry_uri,
            )
            session.add(entry)
            session.add(custody_receipt)
            session.add(
                self._ledger_event(
                    actor,
                    "artifact.registry.persisted",
                    registry_uri,
                    prepared.content_digest,
                    receipt,
                ),
            )
            session.flush()
        return True

    def _build_custody_receipt(
        self,
        *,
        prepared: PreparedArtifact,
        prior_receipt: DeliveryArtifactCustodyReceipt | None,
        receipt_persisted_at: datetime,
        registry_uri: str,
        storage_persisted_at: datetime,
        storage_receipt_ref: str,
    ) -> dict[str, Any]:
        receipt_persisted_at_value = _timestamp(receipt_persisted_at)
        storage_persisted_at_value = _timestamp(storage_persisted_at)
        receipt_token = hashlib.sha256(
            f"delivery-art-custody\0{prepared.content_digest}".encode("utf-8"),
        ).hexdigest()[:24]
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "artifact_type": "delivery_art_custody_receipt",
            "receipt_id": f"artifact-custody-receipt:{receipt_token}",
            "subject": {
                "artifact_type": prepared.artifact_type,
                "artifact_id": prepared.artifact_id,
                "delivery_id": prepared.delivery_id,
                "content_digest": prepared.content_digest,
                "registry_uri": registry_uri,
            },
            "issuer": {
                "owner_repo": "workspace-governance-control-fabric",
                "service_identity_ref": self._service_identity_ref,
                "implementation_ref": self._implementation_ref,
            },
            "storage": {
                "runtime_owner": "platform-engineering",
                "receipt_ref": storage_receipt_ref,
                "persisted_at": storage_persisted_at_value,
            },
            "integrity": {
                "canonicalization": "RFC8785",
                "algorithm": "sha256",
            },
            "custody": {
                "state": "durable",
                "backend": "wgcf-receipt-ledger",
                "uri": "pending",
                "persisted_at": receipt_persisted_at_value,
                "supersedes": (
                    {
                        "uri": prior_receipt.receipt_uri,
                        "digest": prior_receipt.receipt_digest,
                    }
                    if prior_receipt is not None
                    else None
                ),
            },
        }
        receipt_digest = canonical_digest(delivery_art_content_projection(receipt))
        receipt["integrity"]["content_digest"] = receipt_digest
        receipt["custody"]["uri"] = (
            "wgcf://receipts/artifact-custody/"
            f"{receipt_token}-{receipt_digest.removeprefix('sha256:')}.json"
        )
        return receipt

    def _materialize(
        self,
        snapshot: _RegistrySnapshot,
        *,
        resolution: str,
    ) -> ArtifactRegistryResult:
        raw_content = self._storage.read_version(
            snapshot.object_key,
            snapshot.object_version_id,
        )
        if sha256_digest(raw_content) != snapshot.content_digest:
            raise ArtifactStorageIntegrityError(
                "registry-bound object version no longer matches its content digest",
            )
        try:
            content = strict_json_loads(raw_content)
            if not isinstance(content, dict) or canonical_json_bytes(content) != raw_content:
                raise ArtifactStorageIntegrityError(
                    "registry-bound artifact bytes are not canonical",
                )
            prepared = _prepare_stored_content(content, snapshot.content_digest)
        except ArtifactStorageIntegrityError:
            raise
        except (ArtifactRegistryContractError, RecursionError, ValueError) as exc:
            raise ArtifactStorageIntegrityError(
                "registry-bound artifact is not valid canonical Delivery ART content",
            ) from exc
        if (
            prepared.artifact_type != snapshot.artifact_type
            or prepared.artifact_id != snapshot.artifact_id
            or prepared.delivery_id != snapshot.delivery_id
        ):
            raise ArtifactStorageIntegrityError("registry metadata does not match stored artifact identity")
        self._validate_receipt(snapshot)

        artifact = copy.deepcopy(content)
        artifact["integrity"]["content_digest"] = snapshot.content_digest
        artifact["custody"] = {
            "state": "durable",
            "backend": "wgcf-artifact-registry",
            "uri": snapshot.registry_uri,
            "receipt_ref": {
                "uri": snapshot.receipt["custody"]["uri"],
                "digest": snapshot.receipt["integrity"]["content_digest"],
            },
            "persisted_at": snapshot.artifact_persisted_at,
            "supersedes": copy.deepcopy(prepared.supersedes),
        }
        if canonical_digest(delivery_art_content_projection(artifact)) != snapshot.content_digest:
            raise ArtifactStorageIntegrityError("durable artifact reconstruction changed its digest")
        return ArtifactRegistryResult(
            artifact=artifact,
            custody_receipt=copy.deepcopy(snapshot.receipt),
            generation=snapshot.generation,
            resolution=resolution,
        )

    def _validate_receipt(self, snapshot: _RegistrySnapshot) -> None:
        receipt = snapshot.receipt
        integrity = receipt.get("integrity")
        custody = receipt.get("custody")
        issuer = receipt.get("issuer")
        subject = receipt.get("subject")
        storage = receipt.get("storage")
        if not all(
            isinstance(value, dict)
            for value in (integrity, custody, issuer, subject, storage)
        ):
            raise ArtifactStorageIntegrityError("custody receipt is structurally incomplete")
        if set(receipt) != {
            "schema_version",
            "artifact_type",
            "receipt_id",
            "subject",
            "issuer",
            "storage",
            "integrity",
            "custody",
        }:
            raise ArtifactStorageIntegrityError("custody receipt fields do not match the contract")
        if receipt.get("schema_version") != 1 or receipt.get("artifact_type") != "delivery_art_custody_receipt":
            raise ArtifactStorageIntegrityError("custody receipt type does not match the contract")
        if set(integrity) != {"canonicalization", "algorithm", "content_digest"} or (
            integrity.get("canonicalization") != "RFC8785"
            or integrity.get("algorithm") != "sha256"
        ):
            raise ArtifactStorageIntegrityError("custody receipt integrity fields are invalid")
        expected_receipt_digest = canonical_digest(delivery_art_content_projection(receipt))
        if integrity.get("content_digest") != expected_receipt_digest:
            raise ArtifactStorageIntegrityError("custody receipt digest is invalid")
        if receipt.get("receipt_id") != snapshot.receipt_id:
            raise ArtifactStorageIntegrityError("custody receipt identity does not match registry metadata")
        if integrity.get("content_digest") != snapshot.receipt_digest:
            raise ArtifactStorageIntegrityError("custody receipt digest does not match registry metadata")
        receipt_token = hashlib.sha256(
            f"delivery-art-custody\0{snapshot.content_digest}".encode("utf-8"),
        ).hexdigest()[:24]
        expected_receipt_id = f"artifact-custody-receipt:{receipt_token}"
        expected_receipt_uri = (
            "wgcf://receipts/artifact-custody/"
            f"{receipt_token}-{snapshot.receipt_digest.removeprefix('sha256:')}.json"
        )
        if snapshot.receipt_id != expected_receipt_id or snapshot.receipt_uri != expected_receipt_uri:
            raise ArtifactStorageIntegrityError("custody receipt reference is not content-addressed")
        if set(subject) != {
            "artifact_type",
            "artifact_id",
            "delivery_id",
            "content_digest",
            "registry_uri",
        }:
            raise ArtifactStorageIntegrityError("custody receipt subject fields are invalid")
        if (
            subject.get("artifact_type") != snapshot.artifact_type
            or subject.get("artifact_id") != snapshot.artifact_id
            or subject.get("delivery_id") != snapshot.delivery_id
            or subject.get("content_digest") != snapshot.content_digest
            or subject.get("registry_uri") != snapshot.registry_uri
        ):
            raise ArtifactStorageIntegrityError("custody receipt subject does not match registry metadata")
        if issuer != {
            "owner_repo": "workspace-governance-control-fabric",
            "service_identity_ref": self._service_identity_ref,
            "implementation_ref": self._implementation_ref,
        }:
            raise ArtifactStorageIntegrityError("custody receipt issuer does not match this runtime")
        if set(storage) != {"runtime_owner", "receipt_ref", "persisted_at"} or (
            storage.get("runtime_owner") != "platform-engineering"
        ):
            raise ArtifactStorageIntegrityError("custody receipt storage fields are invalid")
        if set(custody) != {"state", "backend", "uri", "persisted_at", "supersedes"} or (
            custody.get("state") != "durable"
            or custody.get("backend") != "wgcf-receipt-ledger"
            or custody.get("persisted_at") != snapshot.receipt_persisted_at
            or custody.get("supersedes") != snapshot.supersedes_receipt_ref
        ):
            raise ArtifactStorageIntegrityError("custody receipt state or lineage is invalid")
        _require_strict_persistence_order(
            "storage.persisted_at",
            storage.get("persisted_at"),
            "custody.persisted_at",
            custody.get("persisted_at"),
        )
        _require_strict_persistence_order(
            "custody.persisted_at",
            custody.get("persisted_at"),
            "artifact custody.persisted_at",
            snapshot.artifact_persisted_at,
        )
        if custody.get("uri") != snapshot.receipt_uri:
            raise ArtifactStorageIntegrityError("custody receipt URI does not match registry metadata")
        if storage.get("receipt_ref") != snapshot.storage_receipt_ref:
            raise ArtifactStorageIntegrityError("storage receipt binding does not match registry metadata")
        expected_storage_ref = self._storage_receipt_ref(
            StoredArtifactObject(
                object_key=snapshot.object_key,
                version_id=snapshot.object_version_id,
                content_digest=snapshot.content_digest,
            ),
            snapshot.content_digest,
        )
        if snapshot.storage_receipt_ref != expected_storage_ref:
            raise ArtifactStorageIntegrityError("registry storage binding is invalid")
        if snapshot.registry_uri != _registry_uri(snapshot.content_digest):
            raise ArtifactStorageIntegrityError("registry URI is not content-addressed")
        if snapshot.object_key != delivery_art_object_key(snapshot.content_digest):
            raise ArtifactStorageIntegrityError("registry object key is not content-addressed")

    def _storage_receipt_ref(
        self,
        binding: StoredArtifactObject,
        content_digest: str,
    ) -> str:
        token = hashlib.sha256(
            (
                f"{self._service_identity_ref}\0{binding.object_key}\0"
                f"{binding.version_id}\0{content_digest}"
            ).encode("utf-8"),
        ).hexdigest()
        return f"platform-storage://receipts/{token}"

    @staticmethod
    def _validate_storage_binding(
        binding: StoredArtifactObject,
        prepared: PreparedArtifact,
    ) -> None:
        if not isinstance(binding, StoredArtifactObject):
            raise ArtifactStorageIntegrityError(
                "artifact storage returned an unsupported object binding",
            )
        if binding.content_digest != prepared.content_digest:
            raise ArtifactStorageIntegrityError(
                "artifact storage binding does not match the content digest",
            )
        if binding.object_key != delivery_art_object_key(prepared.content_digest):
            raise ArtifactStorageIntegrityError(
                "artifact storage binding is not content-addressed",
            )
        if not binding.version_id or binding.version_id == "null":
            raise ArtifactStorageIntegrityError(
                "artifact storage binding has no immutable version ID",
            )

    def _required_snapshot(self, content_digest: str) -> _RegistrySnapshot:
        if not _DIGEST_PATTERN.fullmatch(content_digest):
            raise ArtifactRegistryContractError("content digest must be a lowercase sha256 digest")
        snapshot = self._snapshot_by_digest(content_digest)
        if snapshot is None:
            raise ArtifactRegistryNotFound("artifact digest is not registered")
        return snapshot

    def _snapshot_by_digest(self, content_digest: str) -> _RegistrySnapshot | None:
        with self._session_factory() as session:
            entry = self._entry_by_digest(session, content_digest)
            if entry is None:
                return None
            return self._snapshot(session, entry)

    def _subject_head(self, prepared: PreparedArtifact) -> DeliveryArtifactRegistryEntry | None:
        with self._session_factory() as session:
            entry = self._subject_head_in_session(session, prepared)
            if entry is not None:
                session.expunge(entry)
            return entry

    @staticmethod
    def _entry_by_digest(
        session: Session,
        content_digest: str,
    ) -> DeliveryArtifactRegistryEntry | None:
        return session.scalar(
            select(DeliveryArtifactRegistryEntry).where(
                DeliveryArtifactRegistryEntry.content_digest == content_digest,
            ),
        )

    @staticmethod
    def _subject_head_in_session(
        session: Session,
        prepared: PreparedArtifact,
    ) -> DeliveryArtifactRegistryEntry | None:
        return session.scalar(
            select(DeliveryArtifactRegistryEntry)
            .where(
                DeliveryArtifactRegistryEntry.delivery_id == prepared.delivery_id,
                DeliveryArtifactRegistryEntry.artifact_type == prepared.artifact_type,
                DeliveryArtifactRegistryEntry.artifact_id == prepared.artifact_id,
            )
            .order_by(desc(DeliveryArtifactRegistryEntry.generation))
            .limit(1),
        )

    @staticmethod
    def _snapshot(session: Session, entry: DeliveryArtifactRegistryEntry) -> _RegistrySnapshot:
        receipt = session.scalar(
            select(DeliveryArtifactCustodyReceipt).where(
                DeliveryArtifactCustodyReceipt.registry_uri == entry.registry_uri,
            ),
        )
        if receipt is None:
            raise ArtifactRegistryUnavailable("registry entry has no custody receipt")
        receipt_persisted_at = _database_timestamp(receipt.persisted_at)
        if receipt_persisted_at != receipt.receipt.get("custody", {}).get("persisted_at"):
            raise ArtifactStorageIntegrityError(
                "custody receipt persistence timestamp does not match registry metadata",
            )
        has_supersedes_uri = entry.supersedes_registry_uri is not None
        has_supersedes_digest = entry.supersedes_content_digest is not None
        if has_supersedes_uri != has_supersedes_digest:
            raise ArtifactStorageIntegrityError("registry supersession metadata is incomplete")
        if (entry.generation == 1) == has_supersedes_uri:
            raise ArtifactStorageIntegrityError("registry generation does not match its lineage")
        supersedes_receipt_ref = None
        if entry.supersedes_registry_uri is not None:
            predecessor = session.get(
                DeliveryArtifactRegistryEntry,
                entry.supersedes_registry_uri,
            )
            if predecessor is None:
                raise ArtifactStorageIntegrityError("superseded registry artifact does not exist")
            if (
                predecessor.content_digest != entry.supersedes_content_digest
                or predecessor.delivery_id != entry.delivery_id
                or predecessor.artifact_type != entry.artifact_type
                or predecessor.artifact_id != entry.artifact_id
                or predecessor.generation + 1 != entry.generation
            ):
                raise ArtifactStorageIntegrityError("registry supersession chain is invalid")
            predecessor_receipt = session.scalar(
                select(DeliveryArtifactCustodyReceipt).where(
                    DeliveryArtifactCustodyReceipt.registry_uri == predecessor.registry_uri,
                ),
            )
            if predecessor_receipt is None:
                raise ArtifactStorageIntegrityError("superseded registry artifact has no receipt")
            _require_strict_persistence_order(
                "superseded artifact custody.persisted_at",
                _database_timestamp(predecessor.persisted_at),
                "replacement artifact custody.persisted_at",
                _database_timestamp(entry.persisted_at),
            )
            supersedes_receipt_ref = {
                "uri": predecessor_receipt.receipt_uri,
                "digest": predecessor_receipt.receipt_digest,
            }
        return _RegistrySnapshot(
            artifact_id=entry.artifact_id,
            artifact_type=entry.artifact_type,
            content_digest=entry.content_digest,
            delivery_id=entry.delivery_id,
            generation=entry.generation,
            object_key=entry.object_key,
            object_version_id=entry.object_version_id,
            artifact_persisted_at=_database_timestamp(entry.persisted_at),
            receipt_persisted_at=receipt_persisted_at,
            receipt=copy.deepcopy(receipt.receipt),
            receipt_digest=receipt.receipt_digest,
            receipt_id=receipt.receipt_id,
            receipt_uri=receipt.receipt_uri,
            registry_uri=entry.registry_uri,
            storage_receipt_ref=entry.storage_receipt_ref,
            supersedes_content_digest=entry.supersedes_content_digest,
            supersedes_receipt_ref=supersedes_receipt_ref,
            supersedes_registry_uri=entry.supersedes_registry_uri,
        )

    @staticmethod
    def _validate_supersession(
        prepared: PreparedArtifact,
        head: DeliveryArtifactRegistryEntry | None,
    ) -> None:
        if head is None:
            if prepared.supersedes is not None:
                raise ArtifactRegistryConflict("first artifact generation must not declare supersedes")
            return
        if prepared.supersedes is None:
            raise ArtifactRegistryConflict(
                "a changed artifact subject must supersede its latest durable generation",
            )
        if (
            prepared.supersedes["uri"] != head.registry_uri
            or prepared.supersedes["digest"] != head.content_digest
        ):
            raise ArtifactRegistryConflict(
                "custody.supersedes must bind the latest same-subject artifact",
            )

    @staticmethod
    def _assert_same_registration(
        snapshot: _RegistrySnapshot,
        prepared: PreparedArtifact,
    ) -> None:
        expected_supersedes = (
            {
                "uri": snapshot.supersedes_registry_uri,
                "digest": snapshot.supersedes_content_digest,
            }
            if snapshot.supersedes_registry_uri and snapshot.supersedes_content_digest
            else None
        )
        if (
            snapshot.artifact_type != prepared.artifact_type
            or snapshot.artifact_id != prepared.artifact_id
            or snapshot.delivery_id != prepared.delivery_id
            or expected_supersedes != prepared.supersedes
        ):
            raise ArtifactRegistryConflict("content digest is already bound to another registry identity")

    def _append_ledger_event(
        self,
        actor: str,
        action: str,
        result: ArtifactRegistryResult,
    ) -> None:
        registry = result.to_record()["registry"]
        receipt = result.custody_receipt
        with self._session_factory.begin() as session:
            session.add(
                self._ledger_event(
                    actor,
                    action,
                    registry["artifact_ref"]["uri"],
                    registry["artifact_ref"]["digest"],
                    receipt,
                ),
            )

    @staticmethod
    def _ledger_event(
        actor: str,
        action: str,
        registry_uri: str,
        content_digest: str,
        receipt: dict[str, Any],
    ) -> LedgerEvent:
        return LedgerEvent(
            event_id=f"ledger-event:artifact-registry:{uuid4().hex}",
            actor=actor,
            action=action,
            target=registry_uri,
            outcome="success",
            receipt_refs=[
                {"uri": registry_uri, "digest": content_digest},
                {
                    "receipt_id": receipt["receipt_id"],
                    "uri": receipt["custody"]["uri"],
                    "digest": receipt["integrity"]["content_digest"],
                },
            ],
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ArtifactRegistryUnavailable("registry clock must return a timezone-aware timestamp")
        return value.astimezone(timezone.utc).replace(microsecond=0)

    def _persistence_timeline(
        self,
        head: DeliveryArtifactRegistryEntry | None,
    ) -> _ArtifactPersistenceTimeline:
        # Preserve causal order even when the runtime clock returns the same second.
        storage_persisted_at = self._now()
        if head is not None:
            prior_artifact_persisted_at = _as_utc_datetime(head.persisted_at)
            storage_persisted_at = max(
                storage_persisted_at,
                prior_artifact_persisted_at + timedelta(seconds=1),
            )
        receipt_persisted_at = storage_persisted_at + timedelta(seconds=1)
        artifact_persisted_at = receipt_persisted_at + timedelta(seconds=1)
        return _ArtifactPersistenceTimeline(
            storage=storage_persisted_at,
            receipt=receipt_persisted_at,
            artifact=artifact_persisted_at,
        )


def _prepare_stored_content(content: dict[str, Any], content_digest: str) -> PreparedArtifact:
    envelope = canonical_json_bytes(
        {"artifact_content": content, "content_digest": content_digest},
    )
    return prepare_artifact_registration(envelope)


def _registry_uri(content_digest: str) -> str:
    return f"wgcf://artifacts/delivery-art/sha256/{content_digest.removeprefix('sha256:')}"


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _database_timestamp(value: datetime) -> str:
    return _timestamp(_as_utc_datetime(value))


def _as_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _parse_persistence_timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ArtifactStorageIntegrityError(f"{field_name} is not an RFC3339 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ArtifactStorageIntegrityError(
            f"{field_name} is not an RFC3339 timestamp",
        ) from exc
    if parsed.tzinfo is None:
        raise ArtifactStorageIntegrityError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _require_strict_persistence_order(
    earlier_name: str,
    earlier_value: object,
    later_name: str,
    later_value: object,
) -> None:
    earlier = _parse_persistence_timestamp(earlier_value, earlier_name)
    later = _parse_persistence_timestamp(later_value, later_name)
    if earlier >= later:
        raise ArtifactStorageIntegrityError(f"{earlier_name} must be earlier than {later_name}")


def read_implementation_ref(path: str | Path = SOURCE_REVISION_PATH) -> str:
    source_path = Path(path)
    try:
        value = source_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ArtifactRegistryUnavailable("registry image source revision is unavailable") from exc
    if not _IMPLEMENTATION_REF_PATTERN.fullmatch(value):
        raise ArtifactRegistryUnavailable("registry image source revision is not an exact Git commit")
    return value


def build_artifact_registry_runtime() -> tuple[DeliveryArtifactRegistry, ArtifactRegistryAuthorizer]:
    """Build the default fail-closed dev-integration registry runtime."""

    runtime_profile = os.environ.get(RUNTIME_PROFILE_ENV, "").strip()
    if runtime_profile != "dev-integration":
        raise ArtifactRegistryUnavailable("artifact registry is enabled only in dev-integration")
    service_identity_ref = os.environ.get(SERVICE_IDENTITY_ENV, "").strip()
    if not service_identity_ref:
        raise ArtifactRegistryUnavailable("artifact registry service identity is not configured")
    registry = DeliveryArtifactRegistry(
        session_factory=create_session_factory(),
        storage=S3ArtifactObjectStore(ArtifactStorageSettings.from_environment()),
        service_identity_ref=service_identity_ref,
        implementation_ref=read_implementation_ref(),
    )
    return registry, ArtifactRegistryAuthorizer.from_environment()
