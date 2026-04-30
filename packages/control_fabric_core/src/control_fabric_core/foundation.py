"""Bootstrap-level runtime identity and status helpers.

This module intentionally avoids owning policy. It exposes runtime identity and
local scaffold checks that point back to the workspace-governance authority
contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .database import database_settings


PACKAGE_NAME = "workspace-governance-control-fabric"
PACKAGE_VERSION = "0.1.0"
RUNTIME_REPO = "workspace-governance-control-fabric"
STATUS_BOOTSTRAP = "bootstrap"
AUTHORITY_CONTRACT_REF = (
    "workspace-governance/contracts/governance-control-fabric-operator-surface.yaml"
)
OPERATOR_SURFACE_PATH = Path("docs/operations/operator-surface.md")
WORKER_ENTRYPOINT_PATH = Path("apps/worker/src/wgcf_worker/main.py")


@dataclass(frozen=True)
class AuthorityBoundary:
    repo: str
    owns: str
    fabric_behavior: str


AUTHORITY_BOUNDARIES = (
    AuthorityBoundary(
        repo="workspace-governance",
        owns="contracts, schemas, workspace-root guidance, maturity rules, and routing rules",
        fabric_behavior="consume as authority input and emit receipts; do not mutate directly",
    ),
    AuthorityBoundary(
        repo=RUNTIME_REPO,
        owns="runtime implementation for validation planning, readiness, receipts, ledger, API, worker, and CLI",
        fabric_behavior="implement the approved control-fabric operator surface",
    ),
    AuthorityBoundary(
        repo="platform-engineering",
        owns="approved deployment state, release gates, version pinning, promotion, and runtime adoption",
        fabric_behavior="report gate requirements; do not approve or mutate deployment state",
    ),
    AuthorityBoundary(
        repo="security-architecture",
        owns="security standards, findings, review criteria, and security acceptance posture",
        fabric_behavior="surface security deltas; do not make acceptance decisions",
    ),
    AuthorityBoundary(
        repo="operator-orchestration-service",
        owns="broker-backed workflows, OpenProject adapters, ART writes, blockers, and Review Packets",
        fabric_behavior="route ART and workflow-state mutations through the broker-owned surface",
    ),
)


def repo_required_paths(repo_root: Path) -> dict[str, bool]:
    """Return the bootstrap file checks this repo must satisfy."""

    paths = {
        "README.md": repo_root / "README.md",
        "AGENTS.md": repo_root / "AGENTS.md",
        "pyproject.toml": repo_root / "pyproject.toml",
        "alembic.ini": repo_root / "alembic.ini",
        "migrations/env.py": repo_root / "migrations/env.py",
        "migrations/versions/0001_create_foundation_tables.py": (
            repo_root / "migrations/versions/0001_create_foundation_tables.py"
        ),
        str(WORKER_ENTRYPOINT_PATH): repo_root / WORKER_ENTRYPOINT_PATH,
        str(OPERATOR_SURFACE_PATH): repo_root / OPERATOR_SURFACE_PATH,
    }
    return {name: path.exists() and path.is_file() for name, path in paths.items()}


def status_snapshot(repo_root: str | Path | None = None) -> dict[str, Any]:
    """Return a compact bootstrap status snapshot for operators and tests."""

    root = Path(repo_root or ".").resolve()
    required_paths = repo_required_paths(root)
    return {
        "repo": RUNTIME_REPO,
        "version": PACKAGE_VERSION,
        "status": STATUS_BOOTSTRAP,
        "authority_contract_ref": AUTHORITY_CONTRACT_REF,
        "operator_surface_path": str(OPERATOR_SURFACE_PATH),
        "required_paths": required_paths,
        "ready": all(required_paths.values()),
        "database": database_settings().to_status(),
        "authority_boundaries": [asdict(boundary) for boundary in AUTHORITY_BOUNDARIES],
    }
