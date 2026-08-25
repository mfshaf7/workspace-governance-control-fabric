"""Durable, non-mutating readiness decisions for workspace repositories."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
import yaml
from yaml.constructor import ConstructorError

from .artifact_registry import RUNTIME_PROFILE_ENV, SERVICE_IDENTITY_ENV, read_implementation_ref
from .canonical_json import canonical_digest, delivery_art_content_projection, strict_json_loads
from .database import create_session_factory
from .db.models import LedgerEvent, RepositoryReadinessReceipt
from .repository_readiness_contracts import (
    RepositoryReadinessContractBundle,
    RepositoryReadinessContractError,
    authority_content_digest,
)


MAX_REPOSITORY_READINESS_REQUEST_BYTES = 32_768
WORKSPACE_GOVERNANCE_REPO_ROOT_ENV = "WGCF_WORKSPACE_GOVERNANCE_REPO_ROOT"
_ALLOWED_PROFILE = "dev-integration"
_AUTHORITY_PATH = Path("contracts/repos.yaml")
_RULE_DIRECTORY = Path("contracts/repo-rules")


class RepositoryReadinessError(RuntimeError):
    """Base error for repository readiness."""


class RepositoryReadinessRequestError(RepositoryReadinessError):
    """The request cannot identify one trustworthy repository subject."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RepositoryReadinessNotFound(RepositoryReadinessError):
    """The requested receipt does not exist."""


class RepositoryReadinessUnavailable(RepositoryReadinessError):
    """The evaluator, authority source, or receipt ledger is unavailable."""


@dataclass(frozen=True)
class PreparedRepositoryReadinessRequest:
    profile_id: str
    policy_scope: str
    repo_name: str
    repo_ref: str
    expected_owner_repo: str
    catalog_value_key: str
    expected_authority_digest: str


@dataclass(frozen=True)
class RepositoryAuthorityEvaluation:
    outcome: str
    reason_codes: tuple[str, ...]
    authority_digest: str
    rule_ref: str | None = None
    rule_digest: str | None = None
    repository_lifecycle: str | None = None
    repository_class: str | None = None
    requires_security_bindings: bool | None = None


@dataclass(frozen=True)
class RepositoryReadinessResult:
    receipt: dict[str, Any]
    generation: int
    resolution: str
    reference: dict[str, Any] | None

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
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
        if self.reference is not None:
            record["repository_readiness_reference"] = copy.deepcopy(self.reference)
        return record


def prepare_repository_readiness_request(
    raw_request: bytes,
    *,
    contracts: RepositoryReadinessContractBundle,
) -> PreparedRepositoryReadinessRequest:
    if len(raw_request) > MAX_REPOSITORY_READINESS_REQUEST_BYTES:
        raise RepositoryReadinessRequestError(
            "request-too-large",
            "repository readiness request exceeds the payload limit",
        )
    try:
        request = strict_json_loads(raw_request)
    except (RecursionError, ValueError) as exc:
        raise RepositoryReadinessRequestError("malformed-request", str(exc)) from exc
    try:
        contracts.require_valid("repository_readiness_request", request)
    except RepositoryReadinessContractError as exc:
        raise RepositoryReadinessRequestError("malformed-request", str(exc)) from exc
    return PreparedRepositoryReadinessRequest(
        profile_id=request["profile_id"],
        policy_scope=request["policy_scope"],
        repo_name=request["repo_name"],
        repo_ref=request["repo_ref"],
        expected_owner_repo=request["expected_owner_repo"],
        catalog_value_key=request["catalog_value_key"],
        expected_authority_digest=request["expected_authority_digest"],
    )


class RepositoryReadinessService:
    """Evaluate repository admission truth and issue immutable receipts."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        authority_repo_root: str | Path,
        service_identity_ref: str,
        implementation_ref: str,
        contract_bundle: RepositoryReadinessContractBundle | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        root = Path(authority_repo_root).resolve()
        if not root.is_dir():
            raise RepositoryReadinessUnavailable("Workspace Governance source is unavailable")
        if not service_identity_ref.strip():
            raise RepositoryReadinessUnavailable("readiness service identity is not configured")
        if len(implementation_ref) != 40 or any(
            char not in "0123456789abcdef" for char in implementation_ref
        ):
            raise RepositoryReadinessUnavailable(
                "readiness implementation_ref must be an exact Git commit",
            )
        self._sessions = session_factory
        self._authority_root = root
        self._service_identity_ref = service_identity_ref.strip()
        self._implementation_ref = implementation_ref
        try:
            self._contracts = contract_bundle or RepositoryReadinessContractBundle.load()
        except RepositoryReadinessContractError as exc:
            raise RepositoryReadinessUnavailable(
                "repository readiness contracts are unavailable",
            ) from exc
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(self, raw_request: bytes, *, actor: str) -> RepositoryReadinessResult:
        prepared = prepare_repository_readiness_request(raw_request, contracts=self._contracts)
        try:
            evaluation = self._evaluate(prepared)
            return self._persist(prepared, evaluation, actor=actor)
        except RepositoryReadinessError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryReadinessUnavailable(
                "repository readiness ledger is unavailable",
            ) from exc

    def read(self, receipt_token: str, *, actor: str) -> RepositoryReadinessResult:
        if len(receipt_token) != 24 or any(char not in "0123456789abcdef" for char in receipt_token):
            raise RepositoryReadinessRequestError(
                "invalid-receipt-token",
                "repository readiness receipt token is invalid",
            )
        receipt_id = f"repository-readiness-receipt:{receipt_token}"
        try:
            with self._sessions() as session:
                row = session.get(RepositoryReadinessReceipt, receipt_id)
                if row is None:
                    raise RepositoryReadinessNotFound(
                        "repository readiness receipt was not found",
                    )
                result = self._materialize(row, resolution="read")
            self._append_ledger(actor, "repository.readiness.read", result)
            return result
        except RepositoryReadinessError:
            raise
        except SQLAlchemyError as exc:
            raise RepositoryReadinessUnavailable(
                "repository readiness ledger is unavailable",
            ) from exc

    def _evaluate(
        self,
        prepared: PreparedRepositoryReadinessRequest,
    ) -> RepositoryAuthorityEvaluation:
        authority_bytes = self._read_required(_AUTHORITY_PATH)
        authority_digest = authority_content_digest(authority_bytes)
        if (
            prepared.repo_ref != f"repo://{prepared.repo_name}"
            or prepared.catalog_value_key != prepared.repo_name
        ):
            return RepositoryAuthorityEvaluation(
                "contract_mismatch",
                ("repository-reference-mismatch",),
                authority_digest,
            )
        if prepared.expected_owner_repo != prepared.repo_name:
            return RepositoryAuthorityEvaluation(
                "contract_mismatch",
                ("repository-owner-mismatch",),
                authority_digest,
            )
        if prepared.expected_authority_digest != authority_digest:
            return RepositoryAuthorityEvaluation(
                "stale",
                ("authority-version-stale",),
                authority_digest,
            )

        authority = _load_yaml_mapping(authority_bytes)
        repos = authority.get("repos") if authority is not None else None
        retired_repos = authority.get("retired_repos") if authority is not None else None
        if (
            authority is None
            or authority.get("schema_version") != 1
            or not isinstance(repos, dict)
            or not isinstance(retired_repos, dict)
        ):
            return RepositoryAuthorityEvaluation(
                "contract_mismatch",
                ("authority-contract-invalid",),
                authority_digest,
            )
        repository = repos.get(prepared.repo_name)
        if not isinstance(repository, dict):
            retired_repository = retired_repos.get(prepared.repo_name)
            if (
                isinstance(retired_repository, dict)
                and retired_repository.get("lifecycle") == "retired"
            ):
                return RepositoryAuthorityEvaluation(
                    "retired",
                    ("repository-retired",),
                    authority_digest,
                    repository_lifecycle="retired",
                )
            return RepositoryAuthorityEvaluation(
                "not_admitted",
                ("repository-not-admitted",),
                authority_digest,
            )

        lifecycle = repository.get("lifecycle")
        repo_class = repository.get("repo_class")
        security_required = repository.get("requires_security_bindings")
        if lifecycle == "retired":
            return RepositoryAuthorityEvaluation(
                "retired",
                ("repository-retired",),
                authority_digest,
                repository_lifecycle=lifecycle,
                repository_class=repo_class if isinstance(repo_class, str) else None,
                requires_security_bindings=(
                    security_required if isinstance(security_required, bool) else None
                ),
            )
        if lifecycle != "active":
            return RepositoryAuthorityEvaluation(
                "not_admitted",
                ("repository-not-admitted",),
                authority_digest,
                repository_lifecycle=lifecycle if isinstance(lifecycle, str) else None,
                repository_class=repo_class if isinstance(repo_class, str) else None,
                requires_security_bindings=(
                    security_required if isinstance(security_required, bool) else None
                ),
            )

        rule_path = _RULE_DIRECTORY / f"{prepared.repo_name}.yaml"
        rule_ref = f"repo://workspace-governance/{rule_path.as_posix()}"
        rule_bytes = self._read_optional(rule_path)
        if rule_bytes is None:
            return RepositoryAuthorityEvaluation(
                "contract_mismatch",
                ("repository-rule-missing",),
                authority_digest,
                rule_ref=rule_ref,
                repository_lifecycle=lifecycle,
                repository_class=repo_class if isinstance(repo_class, str) else None,
                requires_security_bindings=(
                    security_required if isinstance(security_required, bool) else None
                ),
            )
        rule_digest = authority_content_digest(rule_bytes)
        rule = _load_yaml_mapping(rule_bytes)
        validation = repository.get("validation_behavior")
        if (
            rule is None
            or rule.get("schema_version") != 1
            or rule.get("repo") != prepared.repo_name
            or rule.get("lifecycle") != lifecycle
            or not isinstance(repo_class, str)
            or not isinstance(security_required, bool)
            or not isinstance(validation, dict)
            or not isinstance(validation.get("posture"), str)
            or not isinstance(validation.get("wgcf_graph_role"), str)
            or not isinstance(validation.get("catalog_refs"), list)
            or not validation["catalog_refs"]
        ):
            return RepositoryAuthorityEvaluation(
                "contract_mismatch",
                ("repository-rule-mismatch",),
                authority_digest,
                rule_ref=rule_ref,
                rule_digest=rule_digest,
                repository_lifecycle=lifecycle,
                repository_class=repo_class if isinstance(repo_class, str) else None,
                requires_security_bindings=(
                    security_required if isinstance(security_required, bool) else None
                ),
            )
        if security_required and not _has_security_posture(rule):
            return RepositoryAuthorityEvaluation(
                "contract_mismatch",
                ("repository-security-posture-missing",),
                authority_digest,
                rule_ref=rule_ref,
                rule_digest=rule_digest,
                repository_lifecycle=lifecycle,
                repository_class=repo_class,
                requires_security_bindings=security_required,
            )
        return RepositoryAuthorityEvaluation(
            "ready",
            ("repository-ready",),
            authority_digest,
            rule_ref=rule_ref,
            rule_digest=rule_digest,
            repository_lifecycle=lifecycle,
            repository_class=repo_class,
            requires_security_bindings=security_required,
        )

    def _persist(
        self,
        prepared: PreparedRepositoryReadinessRequest,
        evaluation: RepositoryAuthorityEvaluation,
        *,
        actor: str,
    ) -> RepositoryReadinessResult:
        decision_key = canonical_digest(
            {
                "schema_version": 1,
                "profile_id": prepared.profile_id,
                "policy_scope": prepared.policy_scope,
                "repo_name": prepared.repo_name,
                "repo_ref": prepared.repo_ref,
                "catalog_value_key": prepared.catalog_value_key,
                "expected_authority_digest": prepared.expected_authority_digest,
                "authority_digest": evaluation.authority_digest,
                "rule_digest": evaluation.rule_digest,
                "contract_digest": self._contracts.contract_digest,
                "implementation_ref": self._implementation_ref,
                "outcome": evaluation.outcome,
                "reason_codes": list(evaluation.reason_codes),
            },
        )
        with self._sessions() as session:
            existing = session.scalar(
                select(RepositoryReadinessReceipt).where(
                    RepositoryReadinessReceipt.decision_key == decision_key,
                ),
            )
            if existing is not None:
                result = self._materialize(existing, resolution="reused")
                self._append_ledger(actor, "repository.readiness.reused", result)
                return result
            prior = session.scalar(
                select(RepositoryReadinessReceipt)
                .where(
                    RepositoryReadinessReceipt.repo_name == prepared.repo_name,
                    RepositoryReadinessReceipt.catalog_value_key == prepared.catalog_value_key,
                    RepositoryReadinessReceipt.profile_id == prepared.profile_id,
                )
                .order_by(desc(RepositoryReadinessReceipt.generation))
                .limit(1),
            )
            if prior is not None:
                session.expunge(prior)

        evaluated_at = _utc_time(self._clock())
        if prior is not None:
            evaluated_at = max(
                evaluated_at,
                _stored_utc(prior.persisted_at) + timedelta(seconds=1),
            )
        persisted_at = evaluated_at + timedelta(seconds=1)
        generation = 1 if prior is None else prior.generation + 1
        token = hashlib.sha256(f"repository-readiness\0{decision_key}".encode("utf-8")).hexdigest()[:24]
        receipt_id = f"repository-readiness-receipt:{token}"
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "artifact_type": "repository_readiness_receipt",
            "receipt_id": receipt_id,
            "generation": generation,
            "subject": {
                "repo_name": prepared.repo_name,
                "repo_ref": prepared.repo_ref,
                "owner_repo": prepared.expected_owner_repo,
                "catalog_value_key": prepared.catalog_value_key,
                "target_scope": f"repo:{prepared.repo_name}",
            },
            "decision": {
                "outcome": evaluation.outcome,
                "linking_allowed": evaluation.outcome == "ready",
                "mutation_authority": "none",
                "reason_codes": list(evaluation.reason_codes),
                "evaluated_at": _timestamp(evaluated_at),
            },
            "authority": {
                "owner_repo": "workspace-governance",
                "record_ref": "repo://workspace-governance/contracts/repos.yaml",
                "record_digest": evaluation.authority_digest,
                "rule_ref": evaluation.rule_ref,
                "rule_digest": evaluation.rule_digest,
                "repository_lifecycle": evaluation.repository_lifecycle,
                "repository_class": evaluation.repository_class,
                "requires_security_bindings": evaluation.requires_security_bindings,
            },
            "policy": {
                "profile_id": prepared.profile_id,
                "scope": prepared.policy_scope,
                "contract_digest": self._contracts.contract_digest,
                "authority_refs": list(self._contracts.authority_refs),
            },
            "issuer": {
                "owner_repo": "workspace-governance-control-fabric",
                "service_identity_ref": self._service_identity_ref,
                "implementation_ref": self._implementation_ref,
            },
            "integrity": {"canonicalization": "RFC8785", "algorithm": "sha256"},
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
            "wgcf://receipts/repository-readiness/"
            f"repository-readiness-receipt-{token}-{receipt_digest.removeprefix('sha256:')}.json"
        )
        try:
            self._contracts.require_valid("repository_readiness_receipt", receipt)
        except RepositoryReadinessContractError as exc:
            raise RepositoryReadinessUnavailable(str(exc)) from exc

        row = RepositoryReadinessReceipt(
            receipt_id=receipt_id,
            receipt_uri=receipt["custody"]["uri"],
            receipt_digest=receipt_digest,
            decision_key=decision_key,
            repo_name=prepared.repo_name,
            repo_ref=prepared.repo_ref,
            catalog_value_key=prepared.catalog_value_key,
            authority_digest=evaluation.authority_digest,
            rule_digest=evaluation.rule_digest,
            profile_id=prepared.profile_id,
            contract_digest=self._contracts.contract_digest,
            implementation_ref=self._implementation_ref,
            generation=generation,
            outcome=evaluation.outcome,
            receipt=receipt,
            evaluated_at=evaluated_at,
            persisted_at=persisted_at,
            supersedes_receipt_uri=prior.receipt_uri if prior is not None else None,
            supersedes_receipt_digest=prior.receipt_digest if prior is not None else None,
        )
        try:
            with self._sessions.begin() as session:
                session.add(row)
                session.add(self._ledger_event(actor, "repository.readiness.persisted", receipt, row.generation))
                session.flush()
        except IntegrityError as exc:
            with self._sessions() as session:
                existing = session.scalar(
                    select(RepositoryReadinessReceipt).where(
                        RepositoryReadinessReceipt.decision_key == decision_key,
                    ),
                )
                if existing is None:
                    raise RepositoryReadinessUnavailable(
                        "repository readiness subject advanced concurrently",
                    ) from exc
                result = self._materialize(existing, resolution="reused")
            self._append_ledger(actor, "repository.readiness.reused", result)
            return result
        return self._materialize(row, resolution="created")

    def _materialize(
        self,
        row: RepositoryReadinessReceipt,
        *,
        resolution: str,
    ) -> RepositoryReadinessResult:
        receipt = copy.deepcopy(row.receipt)
        try:
            self._contracts.require_valid("repository_readiness_receipt", receipt)
        except RepositoryReadinessContractError as exc:
            raise RepositoryReadinessUnavailable(str(exc)) from exc
        digest = canonical_digest(delivery_art_content_projection(receipt))
        token = row.receipt_id.removeprefix("repository-readiness-receipt:")
        expected_uri = (
            "wgcf://receipts/repository-readiness/"
            f"repository-readiness-receipt-{token}-{row.receipt_digest.removeprefix('sha256:')}.json"
        )
        if (
            digest != row.receipt_digest
            or receipt["integrity"]["content_digest"] != row.receipt_digest
            or receipt["custody"]["uri"] != row.receipt_uri
            or row.receipt_uri != expected_uri
            or receipt["receipt_id"] != row.receipt_id
            or receipt["generation"] != row.generation
            or receipt["decision"]["outcome"] != row.outcome
            or receipt["subject"]["repo_name"] != row.repo_name
            or receipt["subject"]["repo_ref"] != row.repo_ref
            or receipt["subject"]["catalog_value_key"] != row.catalog_value_key
            or receipt["authority"]["record_digest"] != row.authority_digest
            or receipt["authority"]["rule_digest"] != row.rule_digest
            or receipt["policy"]["profile_id"] != row.profile_id
            or receipt["policy"]["contract_digest"] != row.contract_digest
            or receipt["issuer"]["implementation_ref"] != row.implementation_ref
        ):
            raise RepositoryReadinessUnavailable(
                "repository readiness receipt ledger integrity failed",
            )
        reference = self._reference(receipt) if row.outcome == "ready" else None
        return RepositoryReadinessResult(receipt, row.generation, resolution, reference)

    def _reference(self, receipt: dict[str, Any]) -> dict[str, Any]:
        reference = {
            "repo_name": receipt["subject"]["repo_name"],
            "repo_ref": receipt["subject"]["repo_ref"],
            "catalog_value_key": receipt["subject"]["catalog_value_key"],
            "receipt": {
                "receipt_id": receipt["receipt_id"],
                "uri": receipt["custody"]["uri"],
                "digest": receipt["integrity"]["content_digest"],
                "issuer": receipt["issuer"]["owner_repo"],
                "target_scope": receipt["subject"]["target_scope"],
                "outcome": receipt["decision"]["outcome"],
                "evaluated_at": receipt["decision"]["evaluated_at"],
                "generation": receipt["generation"],
            },
        }
        try:
            self._contracts.require_valid("repository_readiness_reference", reference)
        except RepositoryReadinessContractError as exc:
            raise RepositoryReadinessUnavailable(str(exc)) from exc
        return reference

    def _read_required(self, relative_path: Path) -> bytes:
        try:
            return (self._authority_root / relative_path).read_bytes()
        except OSError as exc:
            raise RepositoryReadinessUnavailable(
                f"Workspace Governance authority is unavailable: {relative_path}",
            ) from exc

    def _read_optional(self, relative_path: Path) -> bytes | None:
        try:
            return (self._authority_root / relative_path).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RepositoryReadinessUnavailable(
                f"Workspace Governance authority is unavailable: {relative_path}",
            ) from exc

    def _append_ledger(
        self,
        actor: str,
        action: str,
        result: RepositoryReadinessResult,
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
            event_id=f"ledger-event:repository-readiness:{uuid4().hex}",
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
                    "uri": receipt["authority"]["record_ref"],
                    "digest": receipt["authority"]["record_digest"],
                },
            ],
        )


def _load_yaml_mapping(content: bytes) -> dict[str, Any] | None:
    try:
        value = yaml.load(content, Loader=_UniqueKeyLoader) or {}
    except (TypeError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return value if isinstance(value, dict) else None


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _has_security_posture(rule: dict[str, Any]) -> bool:
    security = rule.get("security_requirements")
    return bool(
        isinstance(security, dict)
        and isinstance(security.get("security_owner"), str)
        and security["security_owner"]
        and isinstance(security.get("review_checklist_path"), str)
        and security["review_checklist_path"]
        and isinstance(security.get("review_output_path"), str)
        and security["review_output_path"]
        and isinstance(security.get("required_artifacts"), list)
        and security["required_artifacts"]
    )


def _utc_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise RepositoryReadinessUnavailable("readiness clock must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _stored_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _timestamp(value: datetime) -> str:
    return _utc_time(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_repository_readiness_runtime() -> RepositoryReadinessService:
    if os.environ.get(RUNTIME_PROFILE_ENV, "").strip() != _ALLOWED_PROFILE:
        raise RepositoryReadinessUnavailable(
            "repository readiness is enabled only in dev-integration",
        )
    service_identity_ref = os.environ.get(SERVICE_IDENTITY_ENV, "").strip()
    authority_root = os.environ.get(WORKSPACE_GOVERNANCE_REPO_ROOT_ENV, "").strip()
    if not authority_root:
        raise RepositoryReadinessUnavailable("Workspace Governance source is not configured")
    return RepositoryReadinessService(
        session_factory=create_session_factory(),
        authority_repo_root=authority_root,
        service_identity_ref=service_identity_ref,
        implementation_ref=read_implementation_ref(),
    )
