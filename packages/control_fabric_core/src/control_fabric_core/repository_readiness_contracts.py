"""Digest-pinned contracts for workspace repository readiness."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .canonical_json import canonical_digest, canonicalization_errors


DEFAULT_CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "contracts" / "repository-readiness"
REPOSITORY_READINESS_CONTRACT_ROOT_ENV = "WGCF_REPOSITORY_READINESS_CONTRACT_ROOT"


class RepositoryReadinessContractError(ValueError):
    """The pinned contract bundle or one of its records is invalid."""


@dataclass(frozen=True)
class RepositoryReadinessContractBundle:
    root: Path
    manifest: dict[str, Any]
    contract_digest: str
    authority_validator: Draft202012Validator
    validators: dict[str, Draft202012Validator]

    @classmethod
    def load(cls, root: str | Path | None = None) -> RepositoryReadinessContractBundle:
        resolved_root = Path(
            root or os.environ.get(REPOSITORY_READINESS_CONTRACT_ROOT_ENV) or DEFAULT_CONTRACT_ROOT,
        ).resolve()
        try:
            manifest = json.loads((resolved_root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositoryReadinessContractError(
                "repository readiness contract manifest is unavailable or invalid",
            ) from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise RepositoryReadinessContractError("unsupported repository readiness manifest")
        if manifest.get("contract_id") != "wgcf.repository-readiness.v1":
            raise RepositoryReadinessContractError("repository readiness contract id is invalid")

        authority = manifest.get("authority_source")
        if authority != {
            "repo": "workspace-governance",
            "path": "contracts/repos.yaml",
            "rule_directory": "contracts/repo-rules",
            "schema_path": "contracts/schemas/repos.schema.json",
            "schema_bundle_path": "../workspace-active-inventory/repos.schema.json",
            "schema_commit": "3b89d0f6f50823ada8b9694327692440d10f428e",
            "schema_sha256": "fe8cf65f9d04dc4d246477cb3eb0c2bdc5b351e859e5376e3a610acd2df443c6",
        }:
            raise RepositoryReadinessContractError("repository authority source is invalid")

        authority_schema_path = resolved_root / authority["schema_bundle_path"]
        try:
            authority_schema_bytes = authority_schema_path.read_bytes()
        except OSError as exc:
            raise RepositoryReadinessContractError(
                "repository authority schema is unavailable",
            ) from exc
        if hashlib.sha256(authority_schema_bytes).hexdigest() != authority["schema_sha256"]:
            raise RepositoryReadinessContractError(
                "repository authority schema does not match manifest",
            )
        try:
            authority_schema = json.loads(authority_schema_bytes)
            Draft202012Validator.check_schema(authority_schema)
        except (json.JSONDecodeError, SchemaError) as exc:
            raise RepositoryReadinessContractError(
                "repository authority schema is invalid",
            ) from exc
        format_checker = FormatChecker()
        authority_validator = Draft202012Validator(
            authority_schema,
            format_checker=format_checker,
        )

        consumer = manifest.get("consumer_contract")
        if (
            not isinstance(consumer, dict)
            or consumer.get("repo") != "operator-orchestration-service"
            or not isinstance(consumer.get("commit"), str)
            or len(consumer["commit"]) != 40
            or consumer.get("path")
            != "contracts/catalog/repository-readiness-reference.schema.json"
        ):
            raise RepositoryReadinessContractError("repository readiness consumer is not pinned")

        expected_types = {
            "repository_readiness_request",
            "repository_readiness_receipt",
            "repository_readiness_reference",
        }
        schema_entries = manifest.get("schemas")
        if not isinstance(schema_entries, dict) or set(schema_entries) != expected_types:
            raise RepositoryReadinessContractError("repository readiness schemas are incomplete")

        validators: dict[str, Draft202012Validator] = {}
        for record_type, entry in schema_entries.items():
            if not isinstance(entry, dict):
                raise RepositoryReadinessContractError("repository readiness schema entry is malformed")
            schema_path = resolved_root / str(entry.get("path") or "")
            try:
                schema_bytes = schema_path.read_bytes()
            except OSError as exc:
                raise RepositoryReadinessContractError(
                    f"repository readiness schema is unavailable: {schema_path.name}",
                ) from exc
            if hashlib.sha256(schema_bytes).hexdigest() != entry.get("sha256"):
                raise RepositoryReadinessContractError(
                    f"repository readiness schema does not match manifest: {schema_path.name}",
                )
            try:
                schema = json.loads(schema_bytes)
                Draft202012Validator.check_schema(schema)
            except (json.JSONDecodeError, SchemaError) as exc:
                raise RepositoryReadinessContractError(
                    f"repository readiness schema is invalid: {schema_path.name}",
                ) from exc
            validators[record_type] = Draft202012Validator(schema, format_checker=format_checker)

        if consumer.get("sha256") != schema_entries["repository_readiness_reference"].get("sha256"):
            raise RepositoryReadinessContractError("repository readiness consumer digest is inconsistent")
        authority_refs = manifest.get("authority_refs")
        if (
            not isinstance(authority_refs, list)
            or len(authority_refs) < 2
            or any(not isinstance(ref, str) or not ref for ref in authority_refs)
        ):
            raise RepositoryReadinessContractError("repository readiness authority refs are incomplete")
        return cls(
            root=resolved_root,
            manifest=manifest,
            contract_digest=canonical_digest(manifest),
            authority_validator=authority_validator,
            validators=validators,
        )

    @property
    def authority_refs(self) -> tuple[str, ...]:
        return tuple(self.manifest["authority_refs"])

    def errors(self, record_type: str, record: Any) -> tuple[str, ...]:
        canonical_errors = canonicalization_errors(record)
        if canonical_errors:
            return tuple(canonical_errors)
        validator = self.validators.get(record_type)
        if validator is None:
            return (f"unsupported repository readiness record type {record_type!r}",)
        errors = sorted(
            validator.iter_errors(record),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        return tuple(
            f"{_json_path(error.absolute_path)}: {error.message}"
            for error in errors
        )

    def authority_errors(self, record: Any) -> tuple[str, ...]:
        canonical_errors = canonicalization_errors(record)
        if canonical_errors:
            return tuple(canonical_errors)
        errors = sorted(
            self.authority_validator.iter_errors(record),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        return tuple(
            f"{_json_path(error.absolute_path)}: {error.message}"
            for error in errors
        )

    def require_valid(self, record_type: str, record: Any) -> None:
        errors = self.errors(record_type, record)
        if errors:
            raise RepositoryReadinessContractError("; ".join(errors))


def authority_content_digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _json_path(path: Any) -> str:
    parts = [str(part) for part in path]
    return ".".join(parts) if parts else "<root>"
