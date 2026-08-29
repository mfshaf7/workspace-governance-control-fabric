"""Digest-pinned Workspace Governance repository custody contracts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
import yaml


DEFAULT_REPOSITORY_CUSTODY_CONTRACT_ROOT = (
    Path(__file__).resolve().parents[4] / "contracts" / "repository-custody"
)
REPOSITORY_CUSTODY_CONTRACT_ROOT_ENV = "WGCF_REPOSITORY_CUSTODY_CONTRACT_ROOT"
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class RepositoryCustodyContractError(ValueError):
    """The pinned custody authority bundle or an artifact is invalid."""


@dataclass(frozen=True)
class RepositoryCustodyContractBundle:
    """Verified upstream custody authority and artifact validators."""

    root: Path
    manifest: dict[str, Any]
    authority: dict[str, Any]
    authority_digest: str
    authority_uri: str
    source_commit: str
    validators: dict[str, Draft202012Validator]

    @classmethod
    def load(cls, root: str | Path | None = None) -> RepositoryCustodyContractBundle:
        resolved_root = Path(
            root
            or os.environ.get(REPOSITORY_CUSTODY_CONTRACT_ROOT_ENV)
            or DEFAULT_REPOSITORY_CUSTODY_CONTRACT_ROOT,
        ).resolve()
        try:
            manifest = json.loads((resolved_root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositoryCustodyContractError(
                "repository custody contract manifest is unavailable or invalid",
            ) from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("contract_id") != "wgcf.repository-custody-readiness.v1"
        ):
            raise RepositoryCustodyContractError("repository custody manifest is unsupported")

        source = manifest.get("source")
        if (
            not isinstance(source, dict)
            or source.get("repo") != "workspace-governance"
            or not _COMMIT_PATTERN.fullmatch(str(source.get("commit") or ""))
        ):
            raise RepositoryCustodyContractError(
                "repository custody source must pin an exact Workspace Governance commit",
            )

        authority_entry = manifest.get("authority")
        schema_entries = manifest.get("schemas")
        if not isinstance(authority_entry, dict) or not isinstance(schema_entries, dict):
            raise RepositoryCustodyContractError("repository custody manifest is incomplete")
        expected_schema_types = {
            "repository_custody_contract",
            "repository_custody_request",
            "repository_custody_decision",
            "repository_provider_readback",
            "repository_custody_receipt",
        }
        if set(schema_entries) != expected_schema_types:
            raise RepositoryCustodyContractError("repository custody schemas are incomplete")

        authority_path = _verified_path(resolved_root, authority_entry)
        try:
            authority = yaml.safe_load(authority_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise RepositoryCustodyContractError("repository custody authority is invalid") from exc
        if not isinstance(authority, dict):
            raise RepositoryCustodyContractError("repository custody authority must be an object")

        validators: dict[str, Draft202012Validator] = {}
        for artifact_type, entry in schema_entries.items():
            if not isinstance(entry, dict):
                raise RepositoryCustodyContractError(
                    f"repository custody schema entry {artifact_type} is malformed",
                )
            schema_path = _verified_path(resolved_root, entry)
            try:
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
            except (OSError, json.JSONDecodeError, SchemaError) as exc:
                raise RepositoryCustodyContractError(
                    f"repository custody schema {artifact_type} is invalid",
                ) from exc
            validators[artifact_type] = Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            )

        bundle = cls(
            root=resolved_root,
            manifest=manifest,
            authority=authority,
            authority_digest=f"sha256:{authority_entry['sha256']}",
            authority_uri=str(authority_entry.get("uri") or ""),
            source_commit=str(source["commit"]),
            validators=validators,
        )
        bundle.require_valid("repository_custody_contract", authority)
        bundle._validate_semantics()
        return bundle

    @property
    def first_active_capability(self) -> str:
        return str(self.authority["runtime_activation"]["first_active_capability"])

    @property
    def readiness_capabilities(self) -> frozenset[str]:
        return frozenset((self.first_active_capability, "provision-new"))

    @property
    def provisioning_scope(self) -> dict[str, str]:
        return dict(self.authority["provisioning_controls"]["first_provider_scope"])

    @property
    def policy_version(self) -> str:
        return "repository-custody/v1"

    def errors(self, artifact_type: str, artifact: Any) -> tuple[str, ...]:
        validator = self.validators.get(artifact_type)
        if validator is None:
            return (f"unsupported repository custody artifact {artifact_type!r}",)
        errors = sorted(
            validator.iter_errors(artifact),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        return tuple(
            f"{_json_path(error.absolute_path)}: {error.message}"
            for error in errors
        )

    def require_valid(self, artifact_type: str, artifact: Any) -> None:
        errors = self.errors(artifact_type, artifact)
        if errors:
            raise RepositoryCustodyContractError("; ".join(errors))

    def _validate_semantics(self) -> None:
        roles = self.authority.get("authority_roles", {})
        readiness = roles.get("readiness_authority", {})
        if readiness.get("owner_repo") != "workspace-governance-control-fabric":
            raise RepositoryCustodyContractError("repository custody readiness owner is invalid")
        actions = self.authority.get("actions", {})
        capability = self.first_active_capability
        action = actions.get(capability)
        if (
            capability != "link-existing"
            or not isinstance(action, dict)
            or action.get("provider_mutation") is not False
            or action.get("required_provider_readback") is not True
            or "linked" not in action.get("allowed_to", [])
        ):
            raise RepositoryCustodyContractError(
                "repository custody first capability is not a safe link-existing transition",
            )

        provision = actions.get("provision-new")
        scope = self.authority.get("provisioning_controls", {}).get(
            "first_provider_scope",
            {},
        )
        if (
            not isinstance(provision, dict)
            or provision.get("provider_mutation") is not True
            or provision.get("required_provider_readback") is not True
            or "provisioned" not in provision.get("allowed_to", [])
            or scope.get("provider") != "github"
            or scope.get("provider_host") != "github.com"
            or scope.get("owner_scope") != "organization"
        ):
            raise RepositoryCustodyContractError(
                "repository provisioning readiness is not organization-scoped and readback-bound",
            )


def _verified_path(root: Path, entry: dict[str, Any]) -> Path:
    relative_path = str(entry.get("path") or "")
    expected_digest = str(entry.get("sha256") or "")
    path = (root / relative_path).resolve()
    if not relative_path or not path.is_relative_to(root):
        raise RepositoryCustodyContractError("repository custody contract path is unsafe")
    try:
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RepositoryCustodyContractError(
            f"repository custody contract file {relative_path} is unavailable",
        ) from exc
    if actual_digest != expected_digest:
        raise RepositoryCustodyContractError(
            f"repository custody contract file {relative_path} does not match its manifest digest",
        )
    return path


def _json_path(path: Any) -> str:
    parts = [str(part) for part in path]
    return ".".join(parts) if parts else "<root>"
