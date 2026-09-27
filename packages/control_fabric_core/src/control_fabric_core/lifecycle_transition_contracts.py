"""Digest-pinned contracts for cross-domain lifecycle readiness."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
import yaml

from .canonical_json import canonical_digest, canonicalization_errors


DEFAULT_CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "contracts" / "lifecycle-transition"
LIFECYCLE_TRANSITION_CONTRACT_ROOT_ENV = "WGCF_LIFECYCLE_TRANSITION_CONTRACT_ROOT"


class LifecycleTransitionContractError(ValueError):
    """The pinned lifecycle-transition bundle or a record is invalid."""


@dataclass(frozen=True)
class LifecycleTransitionContractBundle:
    root: Path
    manifest: dict[str, Any]
    lifecycle: dict[str, Any]
    contract_digest: str
    validators: dict[str, Draft202012Validator]

    @classmethod
    def load(cls, root: str | Path | None = None) -> LifecycleTransitionContractBundle:
        resolved_root = Path(
            root or os.environ.get(LIFECYCLE_TRANSITION_CONTRACT_ROOT_ENV) or DEFAULT_CONTRACT_ROOT,
        ).resolve()
        try:
            manifest = json.loads((resolved_root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LifecycleTransitionContractError(
                "lifecycle transition contract manifest is unavailable or invalid",
            ) from exc
        if manifest.get("schema_version") != 1 or manifest.get("contract_id") != (
            "wgcf.lifecycle-transition-readiness.v1"
        ):
            raise LifecycleTransitionContractError("unsupported lifecycle transition contract manifest")

        sources = manifest.get("authority_sources")
        expected_sources = {
            "workspace_governance": "workspace-governance",
            "operator_orchestration_service": "operator-orchestration-service",
        }
        if not isinstance(sources, dict):
            raise LifecycleTransitionContractError("lifecycle transition authority sources are absent")
        for key, repo in expected_sources.items():
            source = sources.get(key)
            if (
                not isinstance(source, dict)
                or source.get("repo") != repo
                or not _is_commit(source.get("commit"))
            ):
                raise LifecycleTransitionContractError(
                    f"lifecycle transition authority source {key!r} is not pinned",
                )

        files = manifest.get("files")
        required_files = {
            "project-lifecycle.yaml",
            "lifecycle-transition-projection.schema.json",
            "prototype-to-delivery.current.valid.json",
            "evaluation.schema.json",
            "readiness.schema.json",
        }
        if not isinstance(files, dict) or set(files) != required_files:
            raise LifecycleTransitionContractError("lifecycle transition contract file set is incomplete")

        loaded_bytes: dict[str, bytes] = {}
        for name, entry in files.items():
            if not isinstance(entry, dict) or entry.get("path") is None:
                raise LifecycleTransitionContractError(f"lifecycle transition file entry {name!r} is invalid")
            path = resolved_root / name
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise LifecycleTransitionContractError(
                    f"lifecycle transition contract file is unavailable: {name}",
                ) from exc
            if hashlib.sha256(content).hexdigest() != entry.get("sha256"):
                raise LifecycleTransitionContractError(
                    f"lifecycle transition contract file does not match manifest: {name}",
                )
            loaded_bytes[name] = content

        try:
            lifecycle_document = yaml.safe_load(loaded_bytes["project-lifecycle.yaml"])
            lifecycle = lifecycle_document["project_lifecycle"]
        except (KeyError, TypeError, yaml.YAMLError) as exc:
            raise LifecycleTransitionContractError("project lifecycle authority is invalid") from exc
        _validate_lifecycle_authority(lifecycle)

        schema_names = (
            "lifecycle-transition-projection.schema.json",
            "evaluation.schema.json",
            "readiness.schema.json",
        )
        schemas: dict[str, dict[str, Any]] = {}
        registry = Registry()
        try:
            for name in schema_names:
                schema = json.loads(loaded_bytes[name])
                Draft202012Validator.check_schema(schema)
                schemas[name] = schema
                registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        except (json.JSONDecodeError, KeyError, SchemaError) as exc:
            raise LifecycleTransitionContractError("lifecycle transition schema is invalid") from exc

        checker = FormatChecker()
        validators = {
            "projection": Draft202012Validator(
                schemas["lifecycle-transition-projection.schema.json"],
                format_checker=checker,
                registry=registry,
            ),
            "evaluation": Draft202012Validator(
                schemas["evaluation.schema.json"],
                format_checker=checker,
                registry=registry,
            ),
            "readiness": Draft202012Validator(
                schemas["readiness.schema.json"],
                format_checker=checker,
                registry=registry,
            ),
        }
        return cls(
            root=resolved_root,
            manifest=manifest,
            lifecycle=lifecycle,
            contract_digest=canonical_digest(manifest),
            validators=validators,
        )

    @property
    def authority_commit(self) -> str:
        return self.manifest["authority_sources"]["workspace_governance"]["commit"]

    @property
    def oos_commit(self) -> str:
        return self.manifest["authority_sources"]["operator_orchestration_service"]["commit"]

    @property
    def authority_ref(self) -> str:
        return f"workspace-governance://contracts/project-lifecycle@{self.authority_commit}"

    def errors(self, record_type: str, record: Any) -> tuple[str, ...]:
        canonical_errors = canonicalization_errors(record)
        if canonical_errors:
            return tuple(canonical_errors)
        validator = self.validators.get(record_type)
        if validator is None:
            return (f"unsupported lifecycle transition record type {record_type!r}",)
        errors = sorted(
            validator.iter_errors(record),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        return tuple(
            f"{_json_path(error.absolute_path)}: {error.message}"
            for error in errors
        )

    def require_valid(self, record_type: str, record: Any) -> None:
        errors = self.errors(record_type, record)
        if errors:
            raise LifecycleTransitionContractError("; ".join(errors))


def _validate_lifecycle_authority(lifecycle: Any) -> None:
    if not isinstance(lifecycle, dict) or lifecycle.get("owner_repo") != "workspace-governance":
        raise LifecycleTransitionContractError("project lifecycle authority owner is invalid")
    roles = lifecycle.get("ownership_roles") or {}
    readiness = roles.get("readiness-evaluation-authority") or {}
    workflow = roles.get("operator-workflow-authority") or {}
    if readiness.get("owner_ref") != "workspace-governance-control-fabric":
        raise LifecycleTransitionContractError("readiness evaluation authority is invalid")
    if workflow.get("owner_ref") != "operator-orchestration-service":
        raise LifecycleTransitionContractError("workflow authority is invalid")
    transitions = lifecycle.get("transitions") or {}
    for transition_id in (
        "proposal-route-incubation",
        "proposal-route-delivery",
        "incubation-promote-delivery",
    ):
        if transition_id not in transitions:
            raise LifecycleTransitionContractError(
                f"required lifecycle transition {transition_id!r} is absent",
            )


def _is_commit(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 40 and all(char in "0123456789abcdef" for char in value)


def _json_path(path: Any) -> str:
    parts = [str(part) for part in path]
    return ".".join(parts) if parts else "<root>"
