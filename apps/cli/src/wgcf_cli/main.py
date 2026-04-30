"""Minimal `wgcf` CLI shell for the scaffold phase."""

from __future__ import annotations

import argparse
import json
from typing import Sequence

from control_fabric_core import AUTHORITY_CONTRACT_REF, RUNTIME_REPO, status_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wgcf",
        description="Workspace Governance Control Fabric operator CLI.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of the compact operator summary.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser("status", help="Show bootstrap runtime status.")
    status_parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root to inspect. Defaults to the current directory.",
    )
    return parser


def render_status_human(snapshot: dict[str, object]) -> str:
    required_paths = snapshot["required_paths"]
    assert isinstance(required_paths, dict)

    path_lines = [
        f"- {path}: {'present' if present else 'missing'}"
        for path, present in sorted(required_paths.items())
    ]
    return "\n".join(
        [
            "Workspace Governance Control Fabric",
            f"repo: {snapshot['repo']}",
            f"status: {snapshot['status']}",
            f"ready: {str(snapshot['ready']).lower()}",
            f"authority: {snapshot['authority_contract_ref']}",
            "required paths:",
            *path_lines,
        ],
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        snapshot = status_snapshot(args.repo_root)
        if args.json:
            print(json.dumps(snapshot, indent=2, sort_keys=True))
        else:
            print(render_status_human(snapshot))
        return 0 if snapshot["ready"] else 1

    parser.error(
        f"{args.command} is not implemented in this scaffold; "
        f"runtime behavior must follow {AUTHORITY_CONTRACT_REF} in {RUNTIME_REPO}.",
    )
    return 2
