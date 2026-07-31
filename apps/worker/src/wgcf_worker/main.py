"""WGCF activity-worker diagnostic and guarded runtime entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Sequence

from control_fabric_core.worker import (
    worker_activation_status,
    worker_status_snapshot,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wgcf-worker",
        description="Workspace Governance Control Fabric worker diagnostic CLI.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of the compact worker summary.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser(
        "status",
        help="Show worker source, registration, and activation status.",
    )
    status_parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root to inspect. Defaults to the current directory.",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print machine-readable JSON for this command.",
    )
    subparsers.add_parser(
        "run",
        help="Run the Temporal activity worker after every activation gate passes.",
    )
    return parser


def render_worker_status_human(snapshot: dict[str, object]) -> str:
    required_paths = snapshot["required_paths"]
    assert isinstance(required_paths, dict)
    temporal = snapshot["temporal"]
    assert isinstance(temporal, dict)
    activation = snapshot["activation"]
    assert isinstance(activation, dict)

    path_lines = [
        f"- {path}: {'present' if present else 'missing'}"
        for path, present in sorted(required_paths.items())
    ]
    return "\n".join(
        [
            "Workspace Governance Control Fabric Worker",
            f"repo: {snapshot['repo']}",
            f"status: {snapshot['status']}",
            f"runtime mode: {snapshot['runtime_mode']}",
            f"ready: {str(snapshot['ready']).lower()}",
            f"temporal task queue: {temporal['task_queue']}",
            f"temporal namespace: {temporal['namespace']}",
            f"temporal-ready boundary: {str(temporal['ready_boundary']).lower()}",
            f"connects to temporal: {str(temporal['connects_to_temporal']).lower()}",
            f"long-running worker: {str(temporal['long_running_worker']).lower()}",
            f"runtime activation authorized: {str(activation['authorized']).lower()}",
            f"registered activities: {', '.join(temporal['registered_activities'])}",
            "required paths:",
            *path_lines,
        ],
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "status":
        snapshot = worker_status_snapshot(args.repo_root)
        if args.json:
            print(json.dumps(snapshot, indent=2, sort_keys=True))
        else:
            print(render_worker_status_human(snapshot))
        return 0 if snapshot["ready"] else 1

    if args.command == "run":
        activation = worker_activation_status()
        if not activation["authorized"]:
            print(
                "WGCF Temporal activity worker refused to start:",
                file=sys.stderr,
            )
            for blocker in activation["blockers"]:
                print(f"- {blocker}", file=sys.stderr)
            return 2
        from .runner import run_worker

        asyncio.run(run_worker())
        return 0

    parser.error(f"{args.command} is not implemented.")
    return 2
