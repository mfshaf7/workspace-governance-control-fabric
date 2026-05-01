"""Workspace validator catalog ingestion for WGCF manifests.

The catalog is owned by ``workspace-governance``. This module translates that
authority input into a runtime governance manifest without redefining policy.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml

from .manifests import MANIFEST_SCHEMA_VERSION, validate_governance_manifest


DEFAULT_CATALOG_RELATIVE_PATH = "workspace-governance/contracts/governance-validator-catalog.yaml"
DEFAULT_PROFILE = "local-read-only"
CATALOG_AUTHORITY_REFS = (
    (
        "workspace-governance-validator-catalog",
        "workspace-governance",
        "contracts/governance-validator-catalog.yaml",
    ),
    (
        "workspace-governance-shadow-parity",
        "workspace-governance",
        "contracts/governance-engine-shadow-parity.yaml",
    ),
    (
        "platform-wgcf-validator-gates",
        "platform-engineering",
        "docs/components/workspace-governance-control-fabric/validator-invocation-gates.md",
    ),
    (
        "security-wgcf-validator-custody-review",
        "security-architecture",
        "docs/reviews/components/2026-05-01-wgcf-validator-invocation-and-artifact-custody.md",
    ),
)
DEFAULT_ALLOWED_SCOPE_PREFIXES = (
    "authority:",
    "component:",
    "projection:",
    "profile:",
    "repo:",
    "validator:",
    "art:",
    "release:",
    "changed-file:",
)


@dataclass(frozen=True)
class CatalogEntrySelection:
    """Catalog entry selected for manifest execution."""

    command: str
    entry_id: str
    owner_repo: str
    safety_class: str
    scopes: tuple[str, ...]
    validation_tier: str
    working_directory_repo: str

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["scopes"] = list(self.scopes)
        return record


@dataclass(frozen=True)
class CatalogEntrySuppression:
    """Catalog entry deliberately excluded from runtime invocation."""

    entry_id: str
    owner_repo: str
    reason: str
    safety_class: str
    wgcf_posture: str

    def to_record(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class CatalogManifestResult:
    """A manifest generated from the workspace-owned validator catalog."""

    catalog_path: str
    manifest: dict[str, Any]
    operator_approved: bool
    profile: str
    selected_entries: tuple[CatalogEntrySelection, ...]
    suppressed_entries: tuple[CatalogEntrySuppression, ...]
    workspace_root: str

    def to_record(self) -> dict[str, Any]:
        return {
            "catalog_path": self.catalog_path,
            "manifest": self.manifest,
            "operator_approved": self.operator_approved,
            "profile": self.profile,
            "selected_entries": [entry.to_record() for entry in self.selected_entries],
            "suppressed_entries": [entry.to_record() for entry in self.suppressed_entries],
            "workspace_root": self.workspace_root,
        }

    def to_summary_record(self) -> dict[str, Any]:
        return {
            "catalog_path": self.catalog_path,
            "manifest_id": self.manifest["manifest_id"],
            "operator_approved": self.operator_approved,
            "profile": self.profile,
            "selected_entry_count": len(self.selected_entries),
            "selected_entries": [entry.to_record() for entry in self.selected_entries],
            "suppressed_entry_count": len(self.suppressed_entries),
            "suppressed_entries": [entry.to_record() for entry in self.suppressed_entries],
            "workspace_root": self.workspace_root,
        }


def default_catalog_path(workspace_root: str | Path) -> Path:
    """Return the default workspace-owned validator catalog path."""

    return Path(workspace_root).resolve() / DEFAULT_CATALOG_RELATIVE_PATH


def load_validator_catalog(path: str | Path) -> dict[str, Any]:
    """Load a workspace governance validator catalog YAML file."""

    catalog_path = Path(path).resolve()
    payload = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"validator catalog must be a mapping: {catalog_path}")
    catalog = payload.get("governance_validator_catalog")
    if not isinstance(catalog, dict):
        raise ValueError("validator catalog missing governance_validator_catalog object")
    return catalog


def build_catalog_governance_manifest(
    *,
    workspace_root: str | Path,
    catalog_path: str | Path | None = None,
    operator_approved: bool = False,
    profile: str = DEFAULT_PROFILE,
) -> CatalogManifestResult:
    """Translate the workspace-owned validator catalog into a WGCF manifest."""

    root = Path(workspace_root).resolve()
    if not root.is_dir():
        raise ValueError(f"workspace_root does not exist or is not a directory: {root}")
    resolved_catalog_path = Path(catalog_path).resolve() if catalog_path else default_catalog_path(root)
    catalog = load_validator_catalog(resolved_catalog_path)
    entries = catalog.get("entries")
    if not isinstance(entries, dict):
        raise ValueError("validator catalog entries must be a mapping")

    authority_refs = _authority_refs(root)
    authority_ref_ids = [ref["authority_id"] for ref in authority_refs]
    components = _representative_components(catalog, authority_ref_ids)
    selected: list[CatalogEntrySelection] = []
    suppressed: list[CatalogEntrySuppression] = []
    validators: list[dict[str, Any]] = []
    repos: dict[str, dict[str, Any]] = {
        repo: {
            "repo_id": repo,
            "repo_name": repo,
            "owner_repo": repo,
            "authority_ref_ids": authority_ref_ids,
        }
        for _, repo, _ in CATALOG_AUTHORITY_REFS
    }

    for entry_id, payload in sorted(entries.items()):
        selection, suppression = _catalog_entry_to_validator(
            entry_id=str(entry_id),
            payload=payload,
            profile=profile,
            workspace_root=root,
            authority_ref_ids=authority_ref_ids,
            operator_approved=operator_approved,
        )
        if suppression:
            suppressed.append(suppression)
            continue
        assert selection is not None
        selected.append(selection)
        validators.append(selection_to_manifest_validator(selection, payload, authority_ref_ids, root, profile, operator_approved))
        repos.setdefault(
            selection.owner_repo,
            {
                "repo_id": selection.owner_repo,
                "repo_name": selection.owner_repo,
                "owner_repo": selection.owner_repo,
                "authority_ref_ids": authority_ref_ids,
            },
        )
        repos.setdefault(
            selection.working_directory_repo,
            {
                "repo_id": selection.working_directory_repo,
                "repo_name": selection.working_directory_repo,
                "owner_repo": selection.working_directory_repo,
                "authority_ref_ids": authority_ref_ids,
            },
        )

    manifest_payload = {
        "authority_refs": authority_refs,
        "components": components,
        "manifest_id": _manifest_id(
            resolved_catalog_path=resolved_catalog_path,
            profile=profile,
            operator_approved=operator_approved,
            selected=selected,
        ),
        "metadata": {
            "catalog_path": str(resolved_catalog_path),
            "operator_approved": operator_approved,
            "profile": profile,
            "selected_catalog_entries": [entry.to_record() for entry in selected],
            "suppressed_catalog_entries": [entry.to_record() for entry in suppressed],
            "workspace_root": str(root),
        },
        "owner_repo": "workspace-governance-control-fabric",
        "projections": [],
        "repos": [repos[repo] for repo in sorted(repos)],
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "validators": validators,
    }
    validation = validate_governance_manifest(manifest_payload)
    if not validation.valid:
        raise ValueError(f"generated catalog manifest is invalid: {'; '.join(validation.errors)}")
    return CatalogManifestResult(
        catalog_path=str(resolved_catalog_path),
        manifest=manifest_payload,
        operator_approved=operator_approved,
        profile=profile,
        selected_entries=tuple(selected),
        suppressed_entries=tuple(suppressed),
        workspace_root=str(root),
    )


def selection_to_manifest_validator(
    selection: CatalogEntrySelection,
    payload: dict[str, Any],
    authority_ref_ids: list[str],
    workspace_root: Path,
    profile: str,
    operator_approved: bool,
) -> dict[str, Any]:
    invocation = payload["wgcf_invocation"]
    executable = _command_executable(selection.command)
    execution_policy = {
        "allowed_executables": [executable],
        "allowed_roots": [str(workspace_root)],
        "catalog_entry_id": selection.entry_id,
        "catalog_surface_id": payload["surface_id"],
        "operator_approved": operator_approved,
        "prefer_current_python": selection.working_directory_repo == "workspace-governance-control-fabric",
        "profile": profile,
        "safety_class": selection.safety_class,
        "working_directory": selection.working_directory_repo,
    }
    for field in (
        "timeout_seconds",
        "retry_count",
        "output_budget_bytes",
        "fail_on_output_budget_exceeded",
    ):
        if field in invocation:
            execution_policy[field] = invocation[field]
    return {
        "authority_ref_ids": authority_ref_ids,
        "check_type": invocation.get("check_type", "command"),
        "command": selection.command,
        "execution_policy": execution_policy,
        "owner_repo": selection.owner_repo,
        "required": True,
        "scopes": list(selection.scopes),
        "validation_tier": selection.validation_tier,
        "validator_id": f"catalog:{selection.entry_id}",
    }


def _catalog_entry_to_validator(
    *,
    authority_ref_ids: list[str],
    entry_id: str,
    operator_approved: bool,
    payload: Any,
    profile: str,
    workspace_root: Path,
) -> tuple[CatalogEntrySelection | None, CatalogEntrySuppression | None]:
    if not isinstance(payload, dict):
        return None, _suppressed(entry_id, {}, "catalog entry is not a mapping")
    invocation = payload.get("wgcf_invocation")
    if not isinstance(invocation, dict) or invocation.get("enabled") is not True:
        return None, _suppressed(entry_id, payload, "entry has no enabled wgcf_invocation")
    if profile not in set(payload.get("allowed_profiles") or []):
        return None, _suppressed(entry_id, payload, f"profile {profile!r} is not allowed for this entry")
    if payload.get("kind") == "support-library":
        return None, _suppressed(entry_id, payload, "support libraries are authority inputs, not invocation targets")
    if payload.get("mutates_authority") is True or payload.get("writes_materialized_outputs") is True:
        return None, _suppressed(entry_id, payload, "mutating or materializing entries are not normal WGCF invocation targets")
    command = str(invocation.get("command") or payload.get("command") or "").strip()
    if not command:
        return None, _suppressed(entry_id, payload, "entry has no command")
    if "<" in command or ">" in command:
        return None, _suppressed(entry_id, payload, "effective command still contains unresolved placeholders")
    if _has_shell_control_token(command):
        return None, _suppressed(entry_id, payload, "effective command requires shell control operators")
    working_repo = str(invocation.get("working_directory_repo") or "").strip()
    if not working_repo:
        return None, _suppressed(entry_id, payload, "wgcf_invocation missing working_directory_repo")
    if not (workspace_root / working_repo).is_dir():
        return None, _suppressed(entry_id, payload, f"working_directory_repo {working_repo!r} is missing")
    scopes = tuple(sorted({str(scope).strip() for scope in invocation.get("scopes") or [] if str(scope).strip()}))
    if not scopes:
        return None, _suppressed(entry_id, payload, "wgcf_invocation has no scopes")
    invalid_scopes = [scope for scope in scopes if not _valid_scope(scope)]
    if invalid_scopes:
        return None, _suppressed(entry_id, payload, "wgcf_invocation has invalid scopes: " + ", ".join(invalid_scopes))
    _command_executable(command)
    return CatalogEntrySelection(
        command=command,
        entry_id=entry_id,
        owner_repo=str(payload.get("owner_repo") or "").strip(),
        safety_class=str(payload.get("safety_class") or "").strip(),
        scopes=scopes,
        validation_tier=str(invocation.get("validation_tier") or "scoped").strip(),
        working_directory_repo=working_repo,
    ), None


def _suppressed(entry_id: str, payload: dict[str, Any], reason: str) -> CatalogEntrySuppression:
    return CatalogEntrySuppression(
        entry_id=entry_id,
        owner_repo=str(payload.get("owner_repo") or "unknown").strip(),
        reason=reason,
        safety_class=str(payload.get("safety_class") or "unknown").strip(),
        wgcf_posture=str(payload.get("wgcf_posture") or "unknown").strip(),
    )


def _representative_components(catalog: dict[str, Any], authority_ref_ids: list[str]) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for scope in catalog.get("representative_scopes") or []:
        planner_scope = str(scope["planner_scope"]).strip()
        if not planner_scope.startswith("component:"):
            continue
        components.append(
            {
                "authority_ref_ids": authority_ref_ids,
                "component_id": planner_scope.removeprefix("component:"),
                "component_type": "representative-validation-scope",
                "impact_scopes": [planner_scope],
                "owner_repo": scope["owner_repo"],
            },
        )
    return components


def _authority_refs(workspace_root: Path) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for authority_id, repo, rel_path in CATALOG_AUTHORITY_REFS:
        path = workspace_root / repo / rel_path
        freshness_status = "current" if path.is_file() else "missing"
        refs.append(
            {
                "authority_id": authority_id,
                "digest": _file_digest(path) if path.is_file() else "sha256:missing",
                "freshness_status": freshness_status,
                "path": rel_path,
                "ref": "local",
                "repo": repo,
            },
        )
    return refs


def _file_digest(path: Path) -> str:
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def _manifest_id(
    *,
    operator_approved: bool,
    profile: str,
    resolved_catalog_path: Path,
    selected: list[CatalogEntrySelection],
) -> str:
    payload = {
        "catalog_digest": _file_digest(resolved_catalog_path),
        "operator_approved": operator_approved,
        "profile": profile,
        "selected_entries": [entry.to_record() for entry in selected],
    }
    digest = sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"catalog-manifest:{digest[:24]}"


def _valid_scope(scope: str) -> bool:
    if scope == "workspace":
        return True
    return any(
        scope.startswith(prefix) and scope.removeprefix(prefix).strip()
        for prefix in DEFAULT_ALLOWED_SCOPE_PREFIXES
    )


def _command_executable(command: str) -> str:
    parts = shlex.split(command)
    while parts and _is_env_assignment(parts[0]):
        parts.pop(0)
    if not parts:
        raise ValueError("catalog invocation command must include an executable")
    return Path(parts[0]).name


def _is_env_assignment(value: str) -> bool:
    if "=" not in value:
        return False
    key, _ = value.split("=", 1)
    return bool(key) and (key[0].isalpha() or key[0] == "_") and all(
        character.isalnum() or character == "_"
        for character in key
    )


def _has_shell_control_token(command: str) -> bool:
    if "$(" in command:
        return True
    try:
        parts = shlex.split(command)
    except ValueError:
        return True
    return any(part in {"&&", "||", "|", ";"} for part in parts)
