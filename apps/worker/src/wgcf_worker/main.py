"""WGCF activity-worker diagnostic and guarded runtime entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Sequence

from control_fabric_core.worker import (
    controlled_proof_worker_activation_status,
    controlled_proof_worker_status_snapshot,
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
    controlled_parser = subparsers.add_parser(
        "controlled-proof",
        help="Inspect or run the isolated commissioning-proof activity worker.",
    )
    controlled_subparsers = controlled_parser.add_subparsers(
        dest="controlled_command",
        required=True,
    )
    controlled_status = controlled_subparsers.add_parser(
        "status",
        help="Show controlled-proof source and authorization status.",
    )
    controlled_status.add_argument(
        "--repo-root",
        default=".",
        help="Repository root to inspect. Defaults to the current directory.",
    )
    controlled_status.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print machine-readable JSON for this command.",
    )
    controlled_subparsers.add_parser(
        "run",
        help="Run only after the permit-derived worker context is authorized.",
    )
    return parser


def render_worker_status_human(
    snapshot: dict[str, object],
    *,
    title: str = "Workspace Governance Control Fabric Worker",
) -> str:
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
            title,
            f"repo: {snapshot['repo']}",
            f"status: {snapshot['status']}",
            f"runtime mode: {snapshot['runtime_mode']}",
            f"ready: {str(snapshot['ready']).lower()}",
            f"temporal task queue: {temporal['task_queue']}",
            f"temporal namespace: {temporal['namespace']}",
            f"temporal worker identity: {temporal['identity']}",
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

    if args.command == "controlled-proof":
        if args.controlled_command == "status":
            snapshot = controlled_proof_worker_status_snapshot(args.repo_root)
            if args.json:
                print(json.dumps(snapshot, indent=2, sort_keys=True))
            else:
                print(
                    render_worker_status_human(
                        snapshot,
                        title="WGCF Controlled Proof Activity Worker",
                    ),
                )
            return 0 if snapshot["ready"] else 1

        if args.controlled_command == "run":
            activation = controlled_proof_worker_activation_status()
            if not activation["authorized"]:
                print(
                    "WGCF controlled-proof activity worker refused to start:",
                    file=sys.stderr,
                )
                for blocker in activation["blockers"]:
                    print(f"- {blocker}", file=sys.stderr)
                return 2
            from .runner import run_controlled_proof_worker

            asyncio.run(run_controlled_proof_worker())
            return 0

    parser.error(f"{args.command} is not implemented.")
    return 2
