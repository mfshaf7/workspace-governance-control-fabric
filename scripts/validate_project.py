#!/usr/bin/env python3
"""Validate the bootstrap Python project scaffold."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path


REQUIRED_PATHS = (
    "pyproject.toml",
    "apps/api/README.md",
    "apps/cli/README.md",
    "apps/cli/src/wgcf_cli/main.py",
    "apps/worker/README.md",
    "packages/control_fabric_core/README.md",
    "packages/control_fabric_core/src/control_fabric_core/foundation.py",
    "docs/architecture/project-structure.md",
    "docs/operations/operator-surface.md",
)


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
    return errors


def validate_imports(repo_root: Path) -> list[str]:
    sys.path.insert(0, str(repo_root / "packages/control_fabric_core/src"))
    sys.path.insert(0, str(repo_root / "apps/cli/src"))

    from control_fabric_core import status_snapshot
    from wgcf_cli.main import build_parser

    snapshot = status_snapshot(repo_root)
    errors: list[str] = []
    if not snapshot["ready"]:
        errors.append("status snapshot is not ready")
    parser = build_parser()
    parsed = parser.parse_args(["status", "--repo-root", str(repo_root)])
    if parsed.command != "status":
        errors.append("wgcf parser did not accept status command")
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
