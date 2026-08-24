"""Fail-closed readiness decisions for Prototype packets entering Delivery."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
import yaml

from .artifact_registry import RUNTIME_PROFILE_ENV, SERVICE_IDENTITY_ENV, read_implementation_ref
from .canonical_json import canonical_digest, delivery_art_content_projection, strict_json_loads
from .database import create_session_factory
from .db.models import LedgerEvent, PrototypeIngressReadinessReceipt
from .prototype_ingress_contracts import (
    PrototypeIngressContractBundle,
    PrototypeIngressContractError,
    prototype_packet_digest,
)


MAX_PROTOTYPE_INGRESS_READINESS_REQUEST_BYTES = 262_144
PROTOTYPE_STUDIO_REPO_ROOT_ENV = "WGCF_PROTOTYPE_STUDIO_REPO_ROOT"
_ALLOWED_PROFILE = "dev-integration"
_PACKET_REF_PREFIX = "record://delivery-packets/"
_BASELINE_REF_PREFIX = "record://design-baselines/"
_GIT_TIMEOUT_SECONDS = 10


class PrototypeIngressReadinessError(RuntimeError):
    """Base error for Prototype ingress readiness."""


class PrototypeIngressReadinessContractError(PrototypeIngressReadinessError):
    """The request cannot identify a trustworthy readiness subject."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class PrototypeIngressReadinessNotFound(PrototypeIngressReadinessError):
    """The requested receipt does not exist."""


class PrototypeIngressReadinessUnavailable(PrototypeIngressReadinessError):
    """The evaluator, source repository, or receipt ledger is unavailable."""


@dataclass(frozen=True)
class PreparedPrototypeIngressRequest:
    profile_id: str
    packet: dict[str, Any]


@dataclass(frozen=True)
class PrototypeIngressReadinessResult:
    receipt: dict[str, Any]
    generation: int
    resolution: str

    def to_record(self) -> dict[str, Any]:
        return {
            "receipt": copy.deepcopy(self.receipt),
            "ledger": {
                "generation": self.generation,
                "resolution": self.resolution,
                "state": "durable",
                "ref": {
                    "uri": self.receipt["custody"]["uri"],
                    "digest": self.receipt["integrity"]["content_digest"],
                },
            },
        }


def prepare_prototype_ingress_request(
    raw_request: bytes,
    *,
    contracts: PrototypeIngressContractBundle,
) -> PreparedPrototypeIngressRequest:
    if len(raw_request) > MAX_PROTOTYPE_INGRESS_READINESS_REQUEST_BYTES:
        raise PrototypeIngressReadinessContractError(
            "request-too-large",
            "Prototype ingress readiness request exceeds the payload limit",
        )
    try:
        request = strict_json_loads(raw_request)
    except (RecursionError, ValueError) as exc:
        raise PrototypeIngressReadinessContractError("malformed-request", str(exc)) from exc
    try:
        contracts.require_valid("prototype_ingress_readiness_request", request)
    except PrototypeIngressContractError as exc:
        raise PrototypeIngressReadinessContractError("malformed-request", str(exc)) from exc
    packet = request["packet"]
    try:
        contracts.require_valid("prototype_delivery_packet", packet)
    except PrototypeIngressContractError as exc:
        raise PrototypeIngressReadinessContractError("malformed-packet", str(exc)) from exc
    return PreparedPrototypeIngressRequest(
        profile_id=request["profile_id"],
        packet=copy.deepcopy(packet),
    )


class PrototypeIngressReadinessService:
    """Verify committed Prototype truth and issue immutable non-mutation receipts."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        source_repo_root: str | Path,
        service_identity_ref: str,
        implementation_ref: str,
        contract_bundle: PrototypeIngressContractBundle | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        root = Path(source_repo_root).resolve()
        if not root.is_dir():
            raise PrototypeIngressReadinessUnavailable(
                "Prototype Studio source repository is unavailable",
            )
        if not service_identity_ref.strip():
            raise PrototypeIngressReadinessUnavailable("readiness service identity is not configured")
        if len(implementation_ref) != 40 or any(char not in "0123456789abcdef" for char in implementation_ref):
            raise PrototypeIngressReadinessUnavailable(
                "readiness implementation_ref must be an exact Git commit",
            )
        self._sessions = session_factory
        self._source_root = root
        self._service_identity_ref = service_identity_ref.strip()
        self._implementation_ref = implementation_ref
        try:
            self._contracts = contract_bundle or PrototypeIngressContractBundle.load()
        except PrototypeIngressContractError as exc:
            raise PrototypeIngressReadinessUnavailable(
                "Prototype ingress readiness contracts are unavailable",
            ) from exc
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(self, raw_request: bytes, *, actor: str) -> PrototypeIngressReadinessResult:
        prepared = prepare_prototype_ingress_request(raw_request, contracts=self._contracts)
        try:
            reason_codes = self._evaluate(prepared)
            return self._persist(prepared, reason_codes, actor=actor)
        except PrototypeIngressReadinessError:
            raise
        except SQLAlchemyError as exc:
            raise PrototypeIngressReadinessUnavailable(
                "Prototype ingress readiness ledger is unavailable",
            ) from exc

    def read(self, receipt_token: str, *, actor: str) -> PrototypeIngressReadinessResult:
        if len(receipt_token) != 24 or any(char not in "0123456789abcdef" for char in receipt_token):
            raise PrototypeIngressReadinessContractError(
                "invalid-receipt-token",
                "Prototype ingress readiness receipt token is invalid",
            )
        receipt_id = f"prototype-ingress-readiness-receipt:{receipt_token}"
        try:
            with self._sessions() as session:
                row = session.get(PrototypeIngressReadinessReceipt, receipt_id)
                if row is None:
                    raise PrototypeIngressReadinessNotFound(
                        "Prototype ingress readiness receipt was not found",
                    )
                result = self._materialize(row, resolution="read")
            self._append_ledger(actor, "prototype-ingress.readiness.read", result)
            return result
        except PrototypeIngressReadinessError:
            raise
        except SQLAlchemyError as exc:
            raise PrototypeIngressReadinessUnavailable(
                "Prototype ingress readiness ledger is unavailable",
            ) from exc

    def _evaluate(self, prepared: PreparedPrototypeIngressRequest) -> tuple[str, ...]:
        packet = prepared.packet
        content = packet["content"]
        source = content["source"]
        expected_digest = prototype_packet_digest(content)
        expected_id = f"{source['prototype_id']}-{expected_digest.removeprefix('sha256:')}"
        if (
            packet["packet_digest"] != expected_digest
            or packet["packet_id"] != expected_id
            or packet["packet_ref"] != f"{_PACKET_REF_PREFIX}{expected_id}"
        ):
            return ("packet-record-mismatch",)
        if prepared.profile_id != _ALLOWED_PROFILE:
            return ("policy-profile-denied",)
        return self._verify_source(packet)

    def _verify_source(self, packet: dict[str, Any]) -> tuple[str, ...]:
        content = packet["content"]
        source = content["source"]
        revision = source["revision"]
        if source["repository"] != "workspace-prototype-studio":
            return ("source-repository-mismatch",)

        head = revision["head_commit"]
        base = revision["base_commit"]
        ref = revision["ref"]
        if not self._git_object_exists(f"{head}^{{commit}}"):
            return ("source-head-missing",)
        if not self._git_object_exists(f"{base}^{{commit}}"):
            return ("source-ancestry-invalid",)
        if not self._git_object_exists(f"{ref}^{{commit}}"):
            return ("source-ref-missing",)
        if not self._git_success("merge-base", "--is-ancestor", base, head):
            return ("source-ancestry-invalid",)
        if not self._git_success("merge-base", "--is-ancestor", head, ref):
            return ("source-projection-stale",)
        if self._git("rev-parse", f"{head}^{{tree}}") != revision["tree"]:
            return ("source-tree-mismatch",)

        packet_path = f"records/delivery-packets/{packet['packet_id']}.json"
        if self._git_object_exists(f"{head}:{packet_path}"):
            return ("source-provenance-self-referential",)
        committed_packet = self._git_show_json(ref, packet_path)
        if committed_packet is None:
            return ("packet-record-missing",)
        if committed_packet != packet:
            return ("packet-record-mismatch",)

        source_registry = self._git_show_yaml(head, "prototypes.yaml")
        current_registry = self._git_show_yaml(ref, "prototypes.yaml")
        if source_registry is None or current_registry is None:
            return ("source-projection-stale",)
        source_prototype = _find_prototype(source_registry, source["prototype_id"])
        current_prototype = _find_prototype(current_registry, source["prototype_id"])
        if source_prototype is None or current_prototype is None:
            return ("source-projection-stale",)
        if (
            (source_registry.get("studio") or {}).get("owner_repo") != source["repository"]
            or source_prototype.get("lifecycle") != "baseline-approved"
            or source_prototype.get("owner") != source["owner"]
            or source_prototype.get("design_baseline_ref") != content["baseline"]["record_ref"]
            or any(source_prototype.get(key) != content["posture"][key] for key in content["posture"])
        ):
            return ("source-projection-stale",)
        if (
            current_prototype.get("lifecycle") != "graduating"
            or current_prototype.get("delivery_packet_ref") != packet["packet_ref"]
        ):
            return ("source-projection-stale",)

        baseline_ref = content["baseline"]["record_ref"]
        if not baseline_ref.startswith(_BASELINE_REF_PREFIX):
            return ("baseline-binding-invalid",)
        baseline_id = baseline_ref.removeprefix(_BASELINE_REF_PREFIX)
        if not baseline_id or "/" in baseline_id or ".." in baseline_id:
            return ("baseline-binding-invalid",)
        baseline = self._git_show_yaml(
            head,
            f"records/design-baselines/{baseline_id}.yaml",
        )
        if (
            baseline is None
            or baseline.get("prototype_id") != source["prototype_id"]
            or baseline.get("baseline_id") != content["baseline"]["baseline_id"]
            or baseline.get("schema_version") != content["baseline"]["schema_version"]
            or baseline.get("decision") != "approved"
            or prototype_packet_digest(baseline) != content["baseline"]["record_digest"]
            or f"{baseline_id}@{prototype_packet_digest(baseline)}" != content["baseline"]["version"]
        ):
            return ("baseline-binding-invalid",)
        return ("eligible",)

    def _persist(
        self,
        prepared: PreparedPrototypeIngressRequest,
        reason_codes: tuple[str, ...],
        *,
        actor: str,
    ) -> PrototypeIngressReadinessResult:
        packet = prepared.packet
        content = packet["content"]
        source = content["source"]
        outcome = "allow" if reason_codes == ("eligible",) else "deny"
        decision_key = canonical_digest(
            {
                "schema_version": 1,
                "profile_id": prepared.profile_id,
                "packet_ref": packet["packet_ref"],
                "packet_digest": packet["packet_digest"],
                "contract_digest": self._contracts.contract_digest,
                "implementation_ref": self._implementation_ref,
                "outcome": outcome,
                "reason_codes": list(reason_codes),
            },
        )
        with self._sessions() as session:
            existing = session.scalar(
                select(PrototypeIngressReadinessReceipt).where(
                    PrototypeIngressReadinessReceipt.decision_key == decision_key,
                ),
            )
            if existing is not None:
                result = self._materialize(existing, resolution="reused")
                self._append_ledger(actor, "prototype-ingress.readiness.reused", result)
                return result
            prior = session.scalar(
                select(PrototypeIngressReadinessReceipt)
                .where(
                    PrototypeIngressReadinessReceipt.source_record_ref == source["record_ref"],
                    PrototypeIngressReadinessReceipt.source_record_version == source["record_version"],
                    PrototypeIngressReadinessReceipt.profile_id == prepared.profile_id,
                )
                .order_by(desc(PrototypeIngressReadinessReceipt.generation))
                .limit(1),
            )
            if prior is not None:
                session.expunge(prior)

        evaluated_at = _utc_time(self._clock())
        if prior is not None:
            evaluated_at = max(evaluated_at, _utc_time(prior.persisted_at) + timedelta(seconds=1))
        persisted_at = evaluated_at + timedelta(seconds=1)
        token = hashlib.sha256(
            f"prototype-ingress-readiness\0{decision_key}".encode("utf-8"),
        ).hexdigest()[:24]
        receipt_id = f"prototype-ingress-readiness-receipt:{token}"
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "artifact_type": "prototype_ingress_readiness_receipt",
            "receipt_id": receipt_id,
            "subject": {
                "source_kind": "prototype",
                "prototype_id": source["prototype_id"],
                "record_ref": source["record_ref"],
                "record_version": source["record_version"],
                "packet_ref": packet["packet_ref"],
                "packet_digest": packet["packet_digest"],
            },
            "decision": {
                "outcome": outcome,
                "target_application_allowed": outcome == "allow",
                "mutation_authority": "none",
                "reason_codes": list(reason_codes),
                "evaluated_at": _timestamp(evaluated_at),
            },
            "policy": {
                "profile_id": prepared.profile_id,
                "target": "workspace-delivery-art",
                "contract_digest": self._contracts.contract_digest,
                "authority_refs": list(self._contracts.authority_refs),
            },
            "evidence": {
                "source_revision": copy.deepcopy(source["revision"] | {"repository": source["repository"]}),
                "baseline": {
                    key: content["baseline"][key]
                    for key in ("record_ref", "version", "record_digest")
                },
                "custody": {
                    key: content["custody"][key]
                    for key in ("classification", "repository_gate_state", "owner", "source_ref")
                },
            },
            "issuer": {
                "owner_repo": "workspace-governance-control-fabric",
                "service_identity_ref": self._service_identity_ref,
                "implementation_ref": self._implementation_ref,
            },
            "integrity": {
                "canonicalization": "RFC8785",
                "algorithm": "sha256",
            },
            "custody": {
                "state": "durable",
                "backend": "wgcf-receipt-ledger",
                "uri": "pending",
                "persisted_at": _timestamp(persisted_at),
                "supersedes": (
                    {"uri": prior.receipt_uri, "digest": prior.receipt_digest}
                    if prior is not None
                    else None
                ),
            },
        }
        receipt_digest = canonical_digest(delivery_art_content_projection(receipt))
        receipt["integrity"]["content_digest"] = receipt_digest
        receipt["custody"]["uri"] = (
            "wgcf://receipts/prototype-ingress-readiness/"
            f"prototype-ingress-readiness-receipt-{token}-"
            f"{receipt_digest.removeprefix('sha256:')}.json"
        )
        try:
            self._contracts.require_valid("prototype_ingress_readiness_receipt", receipt)
        except PrototypeIngressContractError as exc:
            raise PrototypeIngressReadinessUnavailable(str(exc)) from exc

        row = PrototypeIngressReadinessReceipt(
            receipt_id=receipt_id,
            receipt_uri=receipt["custody"]["uri"],
            receipt_digest=receipt_digest,
            decision_key=decision_key,
            packet_ref=packet["packet_ref"],
            packet_digest=packet["packet_digest"],
            source_record_ref=source["record_ref"],
            source_record_version=source["record_version"],
            profile_id=prepared.profile_id,
            contract_digest=self._contracts.contract_digest,
            implementation_ref=self._implementation_ref,
            generation=1 if prior is None else prior.generation + 1,
            outcome=outcome,
            receipt=receipt,
            evaluated_at=evaluated_at,
            persisted_at=persisted_at,
            supersedes_receipt_uri=prior.receipt_uri if prior is not None else None,
            supersedes_receipt_digest=prior.receipt_digest if prior is not None else None,
        )
        try:
            with self._sessions.begin() as session:
                session.add(row)
                session.add(self._ledger_event(actor, "prototype-ingress.readiness.persisted", receipt, row.generation))
                session.flush()
        except IntegrityError as exc:
            with self._sessions() as session:
                existing = session.scalar(
                    select(PrototypeIngressReadinessReceipt).where(
                        PrototypeIngressReadinessReceipt.decision_key == decision_key,
                    ),
                )
                if existing is None:
                    raise PrototypeIngressReadinessUnavailable(
                        "Prototype ingress readiness subject advanced concurrently",
                    ) from exc
                result = self._materialize(existing, resolution="reused")
            self._append_ledger(actor, "prototype-ingress.readiness.reused", result)
            return result
        return self._materialize(row, resolution="created")

    def _materialize(
        self,
        row: PrototypeIngressReadinessReceipt,
        *,
        resolution: str,
    ) -> PrototypeIngressReadinessResult:
        receipt = copy.deepcopy(row.receipt)
        try:
            self._contracts.require_valid("prototype_ingress_readiness_receipt", receipt)
        except PrototypeIngressContractError as exc:
            raise PrototypeIngressReadinessUnavailable(str(exc)) from exc
        digest = canonical_digest(delivery_art_content_projection(receipt))
        token = row.receipt_id.removeprefix("prototype-ingress-readiness-receipt:")
        expected_uri = (
            "wgcf://receipts/prototype-ingress-readiness/"
            f"prototype-ingress-readiness-receipt-{token}-"
            f"{row.receipt_digest.removeprefix('sha256:')}.json"
        )
        if (
            digest != row.receipt_digest
            or receipt["integrity"]["content_digest"] != row.receipt_digest
            or receipt["custody"]["uri"] != row.receipt_uri
            or row.receipt_uri != expected_uri
            or receipt["receipt_id"] != row.receipt_id
            or receipt["issuer"]["implementation_ref"] != row.implementation_ref
        ):
            raise PrototypeIngressReadinessUnavailable(
                "Prototype ingress readiness receipt ledger integrity failed",
            )
        return PrototypeIngressReadinessResult(
            receipt=receipt,
            generation=row.generation,
            resolution=resolution,
        )

    def _append_ledger(
        self,
        actor: str,
        action: str,
        result: PrototypeIngressReadinessResult,
    ) -> None:
        with self._sessions.begin() as session:
            session.add(self._ledger_event(actor, action, result.receipt, result.generation))

    @staticmethod
    def _ledger_event(
        actor: str,
        action: str,
        receipt: dict[str, Any],
        generation: int,
    ) -> LedgerEvent:
        return LedgerEvent(
            event_id=f"ledger-event:prototype-ingress-readiness:{uuid4().hex}",
            actor=actor,
            action=action,
            target=receipt["custody"]["uri"],
            outcome=receipt["decision"]["outcome"],
            receipt_refs=[
                {
                    "receipt_id": receipt["receipt_id"],
                    "uri": receipt["custody"]["uri"],
                    "digest": receipt["integrity"]["content_digest"],
                    "generation": generation,
                },
                {
                    "uri": receipt["subject"]["packet_ref"],
                    "digest": receipt["subject"]["packet_digest"],
                },
            ],
        )

    def _git(self, *args: str) -> str | None:
        try:
            result = subprocess.run(
                self._git_command(*args),
                cwd=self._source_root,
                check=False,
                capture_output=True,
                env=self._git_environment(),
                text=True,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    def _git_success(self, *args: str) -> bool:
        try:
            return subprocess.run(
                self._git_command(*args),
                cwd=self._source_root,
                check=False,
                env=self._git_environment(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_GIT_TIMEOUT_SECONDS,
            ).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _git_command(self, *args: str) -> list[str]:
        return [
            "git",
            "-c",
            f"safe.directory={self._source_root}",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ]

    @staticmethod
    def _git_environment() -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        environment.update(
            {
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_GRAFT_FILE": "/dev/null",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        return environment

    def _git_object_exists(self, object_ref: str) -> bool:
        return self._git_success("cat-file", "-e", object_ref)

    def _git_show_json(self, revision: str, path: str) -> dict[str, Any] | None:
        raw = self._git("show", f"{revision}:{path}")
        if raw is None:
            return None
        try:
            value = strict_json_loads(raw.encode("utf-8"))
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def _git_show_yaml(self, revision: str, path: str) -> dict[str, Any] | None:
        raw = self._git("show", f"{revision}:{path}")
        if raw is None:
            return None
        try:
            value = yaml.safe_load(raw) or {}
        except yaml.YAMLError:
            return None
        return value if isinstance(value, dict) else None


def _find_prototype(registry: dict[str, Any], prototype_id: str) -> dict[str, Any] | None:
    for prototype in registry.get("prototypes") or []:
        if isinstance(prototype, dict) and prototype.get("id") == prototype_id:
            return prototype
    return None


def _utc_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise PrototypeIngressReadinessUnavailable("readiness clock must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _timestamp(value: datetime) -> str:
    return _utc_time(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_prototype_ingress_readiness_runtime() -> PrototypeIngressReadinessService:
    if os.environ.get(RUNTIME_PROFILE_ENV, "").strip() != _ALLOWED_PROFILE:
        raise PrototypeIngressReadinessUnavailable(
            "Prototype ingress readiness is enabled only in dev-integration",
        )
    service_identity_ref = os.environ.get(SERVICE_IDENTITY_ENV, "").strip()
    source_root = os.environ.get(PROTOTYPE_STUDIO_REPO_ROOT_ENV, "").strip()
    if not service_identity_ref:
        raise PrototypeIngressReadinessUnavailable(
            "Prototype ingress readiness service identity is not configured",
        )
    if not source_root:
        raise PrototypeIngressReadinessUnavailable(
            "Prototype Studio source repository is not configured",
        )
    return PrototypeIngressReadinessService(
        session_factory=create_session_factory(),
        source_repo_root=source_root,
        service_identity_ref=service_identity_ref,
        implementation_ref=read_implementation_ref(),
    )
