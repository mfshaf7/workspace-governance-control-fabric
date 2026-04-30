"""Minimal `wgcf` CLI shell for the control-fabric foundation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from control_fabric_core import (
    AUTHORITY_CONTRACT_REF,
    RUNTIME_REPO,
    query_manifest_file,
    status_snapshot,
)


DEFAULT_MANIFEST_PATH = "examples/governance-manifest.example.json"


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
    status_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print machine-readable JSON for this command.",
    )
    graph_parser = subparsers.add_parser("graph", help="Query manifest-derived governance graph views.")
    graph_subparsers = graph_parser.add_subparsers(dest="graph_command", required=True)
    query_parser = graph_subparsers.add_parser("query", help="Query graph nodes and edges for one scope.")
    query_parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve repo-local manifest paths.",
    )
    query_parser.add_argument(
        "--manifest",
        default=DEFAULT_MANIFEST_PATH,
        help="Repo-local governance manifest path.",
    )
    query_parser.add_argument(
        "--scope",
        required=True,
        help="Graph query scope such as repo:<id>, component:<id>, or art:<id>.",
    )
    query_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print machine-readable JSON for this command.",
    )
    return parser


def render_status_human(snapshot: dict[str, object]) -> str:
    required_paths = snapshot["required_paths"]
    assert isinstance(required_paths, dict)

    path_lines = [
        f"- {path}: {'present' if present else 'missing'}"
        for path, present in sorted(required_paths.items())
    ]
    database = snapshot["database"]
    assert isinstance(database, dict)
    return "\n".join(
        [
            "Workspace Governance Control Fabric",
            f"repo: {snapshot['repo']}",
            f"status: {snapshot['status']}",
            f"ready: {str(snapshot['ready']).lower()}",
            f"authority: {snapshot['authority_contract_ref']}",
            f"database: {database['safe_url']}",
            "required paths:",
            *path_lines,
        ],
    )


def render_graph_query_human(record: dict[str, object]) -> str:
    query = record["query"]
    assert isinstance(query, dict)
    summary = query["summary"]
    assert isinstance(summary, dict)
    nodes = query["nodes"]
    edges = query["edges"]
    assert isinstance(nodes, list)
    assert isinstance(edges, list)

    node_lines = [
        f"- {node['node_id']} ({node['node_type']})"
        for node in nodes
    ]
    edge_lines = [
        f"- {edge['source_node_id']} --{edge['edge_type']}--> {edge['target_node_id']}"
        for edge in edges
    ]
    return "\n".join(
        [
            "Workspace Governance Control Fabric Graph Query",
            f"manifest: {record['manifest_path']}",
            f"scope: {query['scope']}",
            f"nodes: {summary['node_count']}",
            f"edges: {summary['edge_count']}",
            "matched nodes:",
            *(node_lines or ["- none"]),
            "matched edges:",
            *(edge_lines or ["- none"]),
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

    if args.command == "graph" and args.graph_command == "query":
        manifest_path = _resolve_manifest_path(args.repo_root, args.manifest)
        query = query_manifest_file(manifest_path, args.scope)
        record = {
            "manifest_path": str(manifest_path.relative_to(Path(args.repo_root).resolve())),
            "query": query.to_record(),
        }
        if args.json:
            print(json.dumps(record, indent=2, sort_keys=True))
        else:
            print(render_graph_query_human(record))
        return 0

    parser.error(
        f"{args.command} is not implemented in this scaffold; "
        f"runtime behavior must follow {AUTHORITY_CONTRACT_REF} in {RUNTIME_REPO}.",
    )
    return 2


def _resolve_manifest_path(repo_root: str, manifest_path: str) -> Path:
    root = Path(repo_root).resolve()
    candidate = Path(manifest_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("manifest path must stay inside the repository root")
    return resolved
