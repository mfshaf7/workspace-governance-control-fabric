"""Authority source snapshot ingestion for WGCF.

Snapshots are digest-only runtime records. They identify upstream authority
surfaces and local repo/profile manifests without copying policy truth into
the control fabric.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from typing import Any


OWNER_MAP_RELATIVE_PATH = "workspace-governance/generated/resolved-owner-map.json"

CORE_AUTHORITY_DECLARATIONS: tuple[tuple[str, str, str], ...] = (
    ("workspace-authority", "workspace-governance", "contracts/repos.yaml"),
    ("workspace-authority", "workspace-governance", "contracts/components.yaml"),
    ("workspace-authority", "workspace-governance", "contracts/products.yaml"),
    ("workspace-authority", "workspace-governance", "contracts/developer-integration-profiles.yaml"),
    ("workspace-authority", "workspace-governance", "contracts/governance-control-fabric-operator-surface.yaml"),
    ("validator-catalog", "workspace-governance", "contracts/governance-validator-catalog.yaml"),
    ("validator-catalog", "workspace-governance", "contracts/validation-matrix.yaml"),
    ("platform-runtime", "platform-engineering", "products/openproject/delivery-art-contract.md"),
    ("platform-runtime", "platform-engineering", "products/openproject/runbooks/check-delivery-art-quality.md"),
    ("platform-runtime", "platform-engineering", "docs/runbooks/dev-integration-profiles.md"),
    ("security-review", "security-architecture", "registers/review-inventory.yaml"),
    ("security-review", "security-architecture", "registers/security-change-record-index.yaml"),
    ("security-review", "security-architecture", "docs/reviews/security-review-checklist.md"),
    ("security-review", "security-architecture", "docs/reviews/components/2026-04-30-workspace-governance-control-fabric-operator-surface.md"),
    ("security-review", "security-architecture", "docs/reviews/components/2026-04-30-workspace-governance-control-fabric-devint-runtime.md"),
    ("operator-workflow", "operator-orchestration-service", "docs/operations/delivery-workflow-operator-surface.md"),
    ("operator-workflow", "operator-orchestration-service", "docs/api/openapi.json"),
    ("operator-workflow", "operator-orchestration-service", "contracts/interface-manifest.json"),
)

REPO_MANIFEST_CANDIDATE_PATHS = (
    "AGENTS.md",
    "README.md",
    "contracts/interface-manifest.json",
    "examples/governance-manifest.example.json",
)

DEVINTEGRATION_PROFILE_FILENAMES = ("profile.yaml", "README.md")


@dataclass(frozen=True)
class SourceRootSnapshot:
    """One local source root observed by a snapshot."""

    exists: bool
    ref: str
    repo: str
    root_path: str

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AuthoritySourceRef:
    """Digest-linked source ref used by graph and validation planning."""

    authority_id: str
    digest: str
    freshness_status: str
    path: str
    ref: str
    repo: str
    source_kind: str

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExcludedSourceRef:
    """Source declaration that could not be included in a snapshot."""

    authority_id: str
    path: str
    reason: str
    repo: str
    source_kind: str

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SourceSnapshot:
    """Digest-only snapshot of authority refs and local source roots."""

    actor: str
    authority_refs: tuple[AuthoritySourceRef, ...]
    excluded_refs: tuple[ExcludedSourceRef, ...]
    source_roots: tuple[SourceRootSnapshot, ...]
    snapshot_id: str
    workspace_root: str

    @property
    def digests(self) -> dict[str, str]:
        return {
            source_ref.authority_id: source_ref.digest
            for source_ref in self.authority_refs
        }

    def to_record(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "authority_refs": [source_ref.to_record() for source_ref in self.authority_refs],
            "digests": self.digests,
            "excluded_refs": [source_ref.to_record() for source_ref in self.excluded_refs],
            "snapshot_id": self.snapshot_id,
            "source_roots": [source_root.to_record() for source_root in self.source_roots],
            "summary": {
                "authority_ref_count": len(self.authority_refs),
                "excluded_ref_count": len(self.excluded_refs),
                "source_root_count": len(self.source_roots),
            },
            "workspace_root": self.workspace_root,
        }


@dataclass(frozen=True)
class SourceDeclaration:
    """Candidate source file that may become an authority ref."""

    path: str
    repo: str
    source_kind: str

    @property
    def authority_id(self) -> str:
        return f"{self.source_kind}:{self.repo}:{self.path}"


def build_source_snapshot(
    workspace_root: str | Path,
    *,
    actor: str = "wgcf-local",
) -> SourceSnapshot:
    """Build a deterministic digest-only snapshot for the current workspace."""

    resolved_workspace_root = Path(workspace_root).resolve()
    owner_map = _load_owner_map(resolved_workspace_root)
    repo_names = _repo_names(owner_map)
    declarations = _source_declarations(resolved_workspace_root, owner_map, repo_names)
    source_roots = tuple(
        SourceRootSnapshot(
            exists=(resolved_workspace_root / repo_name).is_dir(),
            ref=_git_ref(resolved_workspace_root / repo_name),
            repo=repo_name,
            root_path=str((resolved_workspace_root / repo_name).resolve()),
        )
        for repo_name in repo_names
    )

    authority_refs: list[AuthoritySourceRef] = []
    excluded_refs: list[ExcludedSourceRef] = []
    seen_authority_ids: set[str] = set()
    for declaration in declarations:
        if declaration.authority_id in seen_authority_ids:
            continue
        seen_authority_ids.add(declaration.authority_id)
        repo_root = resolved_workspace_root / declaration.repo
        source_path = repo_root / declaration.path
        if not source_path.exists():
            excluded_refs.append(
                ExcludedSourceRef(
                    authority_id=declaration.authority_id,
                    path=declaration.path,
                    reason="missing",
                    repo=declaration.repo,
                    source_kind=declaration.source_kind,
                ),
            )
            continue
        authority_refs.append(
            AuthoritySourceRef(
                authority_id=declaration.authority_id,
                digest=_digest_path(source_path),
                freshness_status="current",
                path=declaration.path,
                ref=_git_ref(repo_root),
                repo=declaration.repo,
                source_kind=declaration.source_kind,
            ),
        )

    authority_refs = sorted(authority_refs, key=lambda item: item.authority_id)
    excluded_refs = sorted(excluded_refs, key=lambda item: item.authority_id)
    snapshot_id = _snapshot_id(actor, resolved_workspace_root, source_roots, authority_refs, excluded_refs)
    return SourceSnapshot(
        actor=actor,
        authority_refs=tuple(authority_refs),
        excluded_refs=tuple(excluded_refs),
        snapshot_id=snapshot_id,
        source_roots=tuple(sorted(source_roots, key=lambda item: item.repo)),
        workspace_root=str(resolved_workspace_root),
    )


def _load_owner_map(workspace_root: Path) -> dict[str, Any]:
    owner_map_path = workspace_root / OWNER_MAP_RELATIVE_PATH
    if not owner_map_path.is_file():
        return {"components": {}, "repos": {}}
    return json.loads(owner_map_path.read_text(encoding="utf-8"))


def _repo_names(owner_map: dict[str, Any]) -> tuple[str, ...]:
    repos = owner_map.get("repos")
    if not isinstance(repos, dict) or not repos:
        return tuple(sorted({
            "workspace-governance-control-fabric",
            *(repo for _, repo, _ in CORE_AUTHORITY_DECLARATIONS),
        }))
    return tuple(sorted(repos))


def _source_declarations(
    workspace_root: Path,
    owner_map: dict[str, Any],
    repo_names: tuple[str, ...],
) -> tuple[SourceDeclaration, ...]:
    declarations = [
        SourceDeclaration(source_kind=source_kind, repo=repo, path=path)
        for source_kind, repo, path in CORE_AUTHORITY_DECLARATIONS
    ]
    declarations.append(
        SourceDeclaration(
            source_kind="workspace-owner-map",
            repo="workspace-governance",
            path="generated/resolved-owner-map.json",
        ),
    )

    for repo_name in repo_names:
        repo_root = workspace_root / repo_name
        for path in REPO_MANIFEST_CANDIDATE_PATHS:
            if (repo_root / path).exists():
                declarations.append(
                    SourceDeclaration(
                        source_kind="repo-manifest",
                        repo=repo_name,
                        path=path,
                    ),
                )
        declarations.extend(_devintegration_declarations(repo_root, repo_name))

    components = owner_map.get("components") if isinstance(owner_map, dict) else None
    if isinstance(components, dict):
        for component_id, component in sorted(components.items()):
            if not isinstance(component, dict):
                continue
            interface_contract = component.get("interface_contract")
            owner_repo = component.get("owner_repo")
            if not isinstance(interface_contract, dict) or not isinstance(owner_repo, str):
                continue
            path = interface_contract.get("path")
            if isinstance(path, str) and path.strip():
                declarations.append(
                    SourceDeclaration(
                        source_kind=f"component-interface:{component_id}",
                        repo=owner_repo,
                        path=path.strip(),
                    ),
                )

    return tuple(declarations)


def _devintegration_declarations(repo_root: Path, repo_name: str) -> tuple[SourceDeclaration, ...]:
    profile_root = repo_root / "dev-integration" / "profiles"
    if not profile_root.is_dir():
        return ()
    declarations: list[SourceDeclaration] = []
    for profile_dir in sorted(path for path in profile_root.iterdir() if path.is_dir()):
        for filename in DEVINTEGRATION_PROFILE_FILENAMES:
            rel_path = profile_dir.relative_to(repo_root) / filename
            if (repo_root / rel_path).exists():
                declarations.append(
                    SourceDeclaration(
                        source_kind="dev-integration-profile",
                        repo=repo_name,
                        path=rel_path.as_posix(),
                    ),
                )
    return tuple(declarations)


def _digest_path(path: Path) -> str:
    if path.is_dir():
        return _digest_directory(path)
    digest = sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _digest_directory(path: Path) -> str:
    digest = sha256()
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        rel_path = child.relative_to(path).as_posix()
        digest.update(rel_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(child.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _git_ref(repo_root: Path) -> str:
    if not (repo_root / ".git").exists():
        return "unversioned"
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _snapshot_id(
    actor: str,
    workspace_root: Path,
    source_roots: tuple[SourceRootSnapshot, ...],
    authority_refs: list[AuthoritySourceRef],
    excluded_refs: list[ExcludedSourceRef],
) -> str:
    payload = {
        "actor": actor,
        "authority_refs": [source_ref.to_record() for source_ref in authority_refs],
        "excluded_refs": [source_ref.to_record() for source_ref in excluded_refs],
        "source_roots": [source_root.to_record() for source_root in source_roots],
        "workspace_root": str(workspace_root),
    }
    digest = sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:32]
    return f"source-snapshot:{digest}"
