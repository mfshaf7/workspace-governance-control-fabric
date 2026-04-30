#!/usr/bin/env python3
"""Validate the bootstrap Python project scaffold."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path


REQUIRED_PATHS = (
    "pyproject.toml",
    "alembic.ini",
    "apps/api/README.md",
    "apps/api/src/wgcf_api/app.py",
    "apps/cli/README.md",
    "apps/cli/src/wgcf_cli/main.py",
    "apps/worker/README.md",
    "packages/control_fabric_core/README.md",
    "packages/control_fabric_core/src/control_fabric_core/database.py",
    "packages/control_fabric_core/src/control_fabric_core/db/models.py",
    "packages/control_fabric_core/src/control_fabric_core/foundation.py",
    "docs/architecture/project-structure.md",
    "docs/operations/operator-surface.md",
    "migrations/env.py",
    "migrations/versions/0001_create_foundation_tables.py",
)

REQUIRED_DB_TABLES = {
    "authority_references",
    "control_receipts",
    "escalation_records",
    "governance_edges",
    "governance_nodes",
    "ledger_events",
    "readiness_decisions",
    "source_snapshots",
    "validation_plans",
    "validation_runs",
}


def validate_paths(repo_root: Path) -> list[str]:
    errors: list[str] = []
    for rel_path in REQUIRED_PATHS:
        path = repo_root / rel_path
        if not path.is_file():
            errors.append(f"missing required scaffold file: {rel_path}")
    return errors


def validate_pyproject(repo_root: Path) -> list[str]:
    errors: list[str] = []
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.is_file():
        return ["missing pyproject.toml"]

    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = pyproject.get("project", {})
    scripts = project.get("scripts", {})
    if project.get("name") != "workspace-governance-control-fabric":
        errors.append("pyproject project.name must be workspace-governance-control-fabric")
    if scripts.get("wgcf") != "wgcf_cli.main:main":
        errors.append("pyproject must expose wgcf = wgcf_cli.main:main")
    if project.get("requires-python") != ">=3.12":
        errors.append("pyproject requires-python must be >=3.12")
    dependencies = set(project.get("dependencies", []))
    if not any(dependency.startswith("alembic") for dependency in dependencies):
        errors.append("pyproject dependencies must include alembic for migration management")
    if not any(dependency.startswith("fastapi") for dependency in dependencies):
        errors.append("pyproject dependencies must include fastapi for the API app")
    if not any(dependency.startswith("psycopg") for dependency in dependencies):
        errors.append("pyproject dependencies must include psycopg for PostgreSQL connectivity")
    if not any(dependency.startswith("sqlalchemy") for dependency in dependencies):
        errors.append("pyproject dependencies must include sqlalchemy for the DB foundation")
    if not any(dependency.startswith("starlette") and "<0.47" in dependency for dependency in dependencies):
        errors.append("pyproject dependencies must pin starlette below 0.47 for TestClient compatibility")
    test_dependencies = set(pyproject.get("project", {}).get("optional-dependencies", {}).get("test", []))
    if not any(dependency.startswith("anyio") and "<4.10" in dependency for dependency in test_dependencies):
        errors.append("pyproject test dependencies must pin anyio below 4.10 for TestClient compatibility")
    return errors


def validate_imports(repo_root: Path) -> list[str]:
    sys.path.insert(0, str(repo_root / "packages/control_fabric_core/src"))
    sys.path.insert(0, str(repo_root / "apps/api/src"))
    sys.path.insert(0, str(repo_root / "apps/cli/src"))

    from control_fabric_core import status_snapshot
    from control_fabric_core.db import metadata
    from wgcf_api import create_app
    from wgcf_cli.main import build_parser

    snapshot = status_snapshot(repo_root)
    errors: list[str] = []
    if not snapshot["ready"]:
        errors.append("status snapshot is not ready")
    parser = build_parser()
    parsed = parser.parse_args(["status", "--repo-root", str(repo_root)])
    if parsed.command != "status":
        errors.append("wgcf parser did not accept status command")
    app = create_app(repo_root)
    if app.title != "Workspace Governance Control Fabric":
        errors.append("FastAPI app title is not the control-fabric title")
    missing_tables = REQUIRED_DB_TABLES.difference(metadata.tables)
    if missing_tables:
        errors.append(f"database metadata missing required tables: {sorted(missing_tables)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    errors = [
        *validate_paths(repo_root),
        *validate_pyproject(repo_root),
        *validate_imports(repo_root),
    ]
    if errors:
        print("ERROR: control-fabric project scaffold validation failed")
        for error in errors:
            print(f"- {error}")
        return 1
    print("control-fabric project scaffold valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
