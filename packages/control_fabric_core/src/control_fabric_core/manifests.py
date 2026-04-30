"""Governance manifest schema and validation helpers.

The manifest is a runtime ingestion contract. It references upstream authority
sources by digest and id; it does not define workspace policy itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MANIFEST_SCHEMA_VERSION = 1
MANIFEST_SECTION_NAMES = ("repos", "components", "validators", "projections")

AUTHORITY_REF_REQUIRED_FIELDS = ("authority_id", "repo", "path", "ref")
SECTION_ID_FIELDS = {
    "components": "component_id",
    "projections": "projection_id",
    "repos": "repo_id",
    "validators": "validator_id",
}
SECTION_REQUIRED_FIELDS = {
    "components": ("component_id", "owner_repo", "component_type", "authority_ref_ids"),
    "projections": ("projection_id", "owner_repo", "source_ref_ids", "output_ref"),
    "repos": ("repo_id", "owner_repo", "authority_ref_ids"),
    "validators": ("validator_id", "owner_repo", "command", "scopes", "authority_ref_ids"),
}


@dataclass(frozen=True)
class ManifestValidationResult:
    """Dependency-free validation result for manifest ingestion preflight."""

    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_status(self) -> dict[str, Any]:
        return {
            "errors": list(self.errors),
            "valid": self.valid,
        }


def governance_manifest_schema() -> dict[str, Any]:
    """Return the JSON Schema for runtime governance manifests."""

    non_empty_string = {"minLength": 1, "type": "string"}
    authority_ref = {
        "additionalProperties": True,
        "properties": {
            "authority_id": non_empty_string,
            "digest": non_empty_string,
            "freshness_status": non_empty_string,
            "path": non_empty_string,
            "ref": non_empty_string,
            "repo": non_empty_string,
        },
        "required": list(AUTHORITY_REF_REQUIRED_FIELDS),
        "type": "object",
    }
    authority_ref_ids = {
        "items": non_empty_string,
        "minItems": 1,
        "type": "array",
        "uniqueItems": True,
    }
    output_ref = {
        "additionalProperties": True,
        "properties": {
            "path": non_empty_string,
            "repo": non_empty_string,
        },
        "required": ["repo", "path"],
        "type": "object",
    }
    impact_scopes = {
        "items": non_empty_string,
        "type": "array",
        "uniqueItems": True,
    }
    reuse_policy = {
        "additionalProperties": True,
        "properties": {
            "freshness_seconds": {"minimum": 0, "type": "integer"},
            "invalidate_on_authority_change": {"type": "boolean"},
            "safe_to_reuse": {"type": "boolean"},
        },
        "type": "object",
    }
    execution_policy = {
        "additionalProperties": True,
        "properties": {
            "fail_on_output_budget_exceeded": {"type": "boolean"},
            "output_budget_bytes": {"minimum": 0, "type": "integer"},
            "retry_count": {"minimum": 0, "type": "integer"},
            "timeout_seconds": {"minimum": 1, "type": "integer"},
        },
        "type": "object",
    }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://workspace-governance.local/schemas/governance-manifest.schema.json",
        "additionalProperties": True,
        "description": (
            "Runtime ingestion manifest for the Workspace Governance Control Fabric. "
            "Policy truth stays in upstream authority refs."
        ),
        "properties": {
            "authority_refs": {
                "items": authority_ref,
                "minItems": 1,
                "type": "array",
            },
            "components": {
                "items": {
                    "additionalProperties": True,
                    "properties": {
                        "authority_ref_ids": authority_ref_ids,
                        "component_id": non_empty_string,
                        "component_type": non_empty_string,
                        "impact_scopes": impact_scopes,
                        "owner_repo": non_empty_string,
                        "source_paths": {
                            "items": non_empty_string,
                            "type": "array",
                            "uniqueItems": True,
                        },
                    },
                    "required": list(SECTION_REQUIRED_FIELDS["components"]),
                    "type": "object",
                },
                "type": "array",
            },
            "manifest_id": non_empty_string,
            "metadata": {
                "additionalProperties": True,
                "type": "object",
            },
            "owner_repo": non_empty_string,
            "projections": {
                "items": {
                    "additionalProperties": True,
                    "properties": {
                        "output_ref": output_ref,
                        "owner_repo": non_empty_string,
                        "projection_id": non_empty_string,
                        "source_ref_ids": authority_ref_ids,
                    },
                    "required": list(SECTION_REQUIRED_FIELDS["projections"]),
                    "type": "object",
                },
                "type": "array",
            },
            "repos": {
                "items": {
                    "additionalProperties": True,
                    "properties": {
                        "authority_ref_ids": authority_ref_ids,
                        "owner_repo": non_empty_string,
                        "repo_id": non_empty_string,
                        "repo_name": non_empty_string,
                        "source_paths": {
                            "items": non_empty_string,
                            "type": "array",
                            "uniqueItems": True,
                        },
                        "impact_scopes": impact_scopes,
                    },
                    "required": list(SECTION_REQUIRED_FIELDS["repos"]),
                    "type": "object",
                },
                "type": "array",
            },
            "schema_version": {"const": MANIFEST_SCHEMA_VERSION},
            "validators": {
                "items": {
                    "additionalProperties": True,
                    "properties": {
                        "authority_ref_ids": authority_ref_ids,
                        "command": non_empty_string,
                        "execution_policy": execution_policy,
                        "owner_repo": non_empty_string,
                        "reuse_policy": reuse_policy,
                        "scopes": authority_ref_ids,
                        "validator_id": non_empty_string,
                    },
                    "required": list(SECTION_REQUIRED_FIELDS["validators"]),
                    "type": "object",
                },
                "type": "array",
            },
        },
        "required": [
            "schema_version",
            "manifest_id",
            "owner_repo",
            "authority_refs",
            *MANIFEST_SECTION_NAMES,
        ],
        "title": "Workspace Governance Control Fabric Manifest",
        "type": "object",
    }


def validate_governance_manifest(manifest: Any) -> ManifestValidationResult:
    """Validate the core manifest invariants used before graph ingestion."""

    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ManifestValidationResult(errors=("manifest must be an object",))

    _require_value(manifest, "schema_version", errors)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        errors.append(f"schema_version must equal {MANIFEST_SCHEMA_VERSION}")
    for field_name in ("manifest_id", "owner_repo"):
        _require_non_empty_string(manifest, field_name, errors)

    authority_refs = _require_list(manifest, "authority_refs", errors, min_items=1)
    authority_ids = _validate_authority_refs(authority_refs, errors)

    for section_name in MANIFEST_SECTION_NAMES:
        section_items = _require_list(manifest, section_name, errors)
        _validate_section(section_name, section_items, authority_ids, errors)

    return ManifestValidationResult(errors=tuple(errors))


def manifest_entity_ids(manifest: dict[str, Any]) -> dict[str, list[str]]:
    """Return section ids in stable order for graph planning diagnostics."""

    result: dict[str, list[str]] = {}
    for section_name in MANIFEST_SECTION_NAMES:
        id_field = SECTION_ID_FIELDS[section_name]
        result[section_name] = [
            str(item[id_field]).strip()
            for item in manifest.get(section_name, [])
            if isinstance(item, dict) and _is_non_empty_string(item.get(id_field))
        ]
    return result


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_value(item: dict[str, Any], field_name: str, errors: list[str]) -> None:
    if field_name not in item:
        errors.append(f"{field_name} is required")


def _require_non_empty_string(
    item: dict[str, Any],
    field_name: str,
    errors: list[str],
    *,
    path: str = "",
) -> None:
    prefix = f"{path}." if path else ""
    if not _is_non_empty_string(item.get(field_name)):
        errors.append(f"{prefix}{field_name} must be a non-empty string")


def _require_list(
    item: dict[str, Any],
    field_name: str,
    errors: list[str],
    *,
    min_items: int = 0,
    path: str = "",
) -> list[Any]:
    prefix = f"{path}." if path else ""
    value = item.get(field_name)
    if not isinstance(value, list):
        errors.append(f"{prefix}{field_name} must be a list")
        return []
    if len(value) < min_items:
        errors.append(f"{prefix}{field_name} must contain at least {min_items} item(s)")
    return value


def _validate_authority_refs(authority_refs: list[Any], errors: list[str]) -> set[str]:
    authority_ids: set[str] = set()
    for index, authority_ref in enumerate(authority_refs):
        path = f"authority_refs[{index}]"
        if not isinstance(authority_ref, dict):
            errors.append(f"{path} must be an object")
            continue
        for field_name in AUTHORITY_REF_REQUIRED_FIELDS:
            _require_non_empty_string(authority_ref, field_name, errors, path=path)
        authority_id = authority_ref.get("authority_id")
        if not _is_non_empty_string(authority_id):
            continue
        normalized_authority_id = authority_id.strip()
        if normalized_authority_id in authority_ids:
            errors.append(f"{path}.authority_id duplicates {normalized_authority_id}")
        authority_ids.add(normalized_authority_id)
    return authority_ids


def _validate_section(
    section_name: str,
    section_items: list[Any],
    authority_ids: set[str],
    errors: list[str],
) -> None:
    id_field = SECTION_ID_FIELDS[section_name]
    seen_ids: set[str] = set()
    for index, section_item in enumerate(section_items):
        path = f"{section_name}[{index}]"
        if not isinstance(section_item, dict):
            errors.append(f"{path} must be an object")
            continue
        for field_name in SECTION_REQUIRED_FIELDS[section_name]:
            if field_name in {"authority_ref_ids", "source_ref_ids", "scopes"}:
                _validate_string_list(section_item, field_name, errors, path=path)
            elif field_name == "output_ref":
                _validate_output_ref(section_item.get(field_name), errors, path=f"{path}.{field_name}")
            else:
                _require_non_empty_string(section_item, field_name, errors, path=path)

        entity_id = section_item.get(id_field)
        if _is_non_empty_string(entity_id):
            normalized_entity_id = entity_id.strip()
            if normalized_entity_id in seen_ids:
                errors.append(f"{path}.{id_field} duplicates {normalized_entity_id}")
            seen_ids.add(normalized_entity_id)

        ref_field_name = "source_ref_ids" if section_name == "projections" else "authority_ref_ids"
        _validate_known_authority_ids(
            section_item.get(ref_field_name),
            authority_ids,
            errors,
            path=f"{path}.{ref_field_name}",
        )


def _validate_string_list(item: dict[str, Any], field_name: str, errors: list[str], *, path: str) -> list[str]:
    values = _require_list(item, field_name, errors, min_items=1, path=path)
    normalized_values: list[str] = []
    seen_values: set[str] = set()
    for index, value in enumerate(values):
        item_path = f"{path}.{field_name}[{index}]"
        if not _is_non_empty_string(value):
            errors.append(f"{item_path} must be a non-empty string")
            continue
        normalized_value = value.strip()
        if normalized_value in seen_values:
            errors.append(f"{item_path} duplicates {normalized_value}")
        seen_values.add(normalized_value)
        normalized_values.append(normalized_value)
    return normalized_values


def _validate_known_authority_ids(
    values: Any,
    authority_ids: set[str],
    errors: list[str],
    *,
    path: str,
) -> None:
    if not isinstance(values, list):
        return
    for value in values:
        if _is_non_empty_string(value) and value.strip() not in authority_ids:
            errors.append(f"{path} references unknown authority id {value.strip()}")


def _validate_output_ref(output_ref: Any, errors: list[str], *, path: str) -> None:
    if not isinstance(output_ref, dict):
        errors.append(f"{path} must be an object")
        return
    for field_name in ("repo", "path"):
        _require_non_empty_string(output_ref, field_name, errors, path=path)
