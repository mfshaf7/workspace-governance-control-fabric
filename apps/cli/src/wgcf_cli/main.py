"""Minimal `wgcf` CLI shell for the control-fabric foundation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from control_fabric_core import (
    AUTHORITY_CONTRACT_REF,
    DEFAULT_ARTIFACT_ROOT,
    DEFAULT_LEDGER_PATH,
    DEFAULT_RECEIPT_DIR,
    RUNTIME_REPO,
    build_operator_validation_plan,
    list_control_receipts,
    query_manifest_file,
    run_operator_validation_check,
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
    plan_parser = subparsers.add_parser("plan", help="Build a validation plan without running checks.")
    _add_repo_manifest_scope_args(plan_parser)
    plan_parser.add_argument(
        "--tier",
        default="scoped",
        help="Validation tier: smoke, scoped, full, or release.",
    )
    plan_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print machine-readable JSON for this command.",
    )
    check_parser = subparsers.add_parser("check", help="Run a bounded validation plan and write a receipt.")
    _add_repo_manifest_scope_args(check_parser)
    check_parser.add_argument(
        "--tier",
        default="scoped",
        help="Validation tier: smoke, scoped, full, or release.",
    )
    check_parser.add_argument(
        "--artifact-root",
        default=DEFAULT_ARTIFACT_ROOT,
        help="Repo-local directory for raw validation artifacts.",
    )
    check_parser.add_argument(
        "--receipt-dir",
        default=DEFAULT_RECEIPT_DIR,
        help="Repo-local directory for compact receipt JSON files.",
    )
    check_parser.add_argument(
        "--ledger",
        default=DEFAULT_LEDGER_PATH,
        help="Repo-local JSONL ledger file.",
    )
    check_parser.add_argument(
        "--actor",
        default="wgcf-local",
        help="Operator or automation actor recorded in the ledger event.",
    )
    check_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print machine-readable JSON for this command.",
    )
    receipts_parser = subparsers.add_parser("receipts", help="Inspect local validation receipts.")
    receipts_subparsers = receipts_parser.add_subparsers(dest="receipts_command", required=True)
    list_parser = receipts_subparsers.add_parser("list", help="List compact receipt metadata.")
    list_parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve repo-local receipt paths.",
    )
    list_parser.add_argument(
        "--receipt-dir",
        default=DEFAULT_RECEIPT_DIR,
        help="Repo-local directory for compact receipt JSON files.",
    )
    list_parser.add_argument(
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


def render_validation_plan_human(record: dict[str, object]) -> str:
    plan = record["plan"]
    assert isinstance(plan, dict)
    decision = plan["decision"]
    target = plan["target"]
    checks = plan["checks"]
    assert isinstance(decision, dict)
    assert isinstance(target, dict)
    assert isinstance(checks, list)

    check_lines = [
        f"- {check['validator_id']} ({check['execution_mode']}, {check['tier']})"
        for check in checks
    ]
    return "\n".join(
        [
            "Workspace Governance Control Fabric Validation Plan",
            f"manifest: {record['manifest_path']}",
            f"target: {target['scope']}",
            f"tier: {plan['tier']}",
            f"decision: {decision['outcome']}",
            f"operator review: {str(decision['requires_operator_review']).lower()}",
            f"checks: {len(checks)}",
            *(check_lines or ["- none"]),
        ],
    )


def render_check_human(record: dict[str, object]) -> str:
    receipt = record["receipt"]
    plan = record["plan"]
    assert isinstance(receipt, dict)
    assert isinstance(plan, dict)
    check_results = receipt["check_results"]
    assert isinstance(check_results, list)
    result_lines = [
        f"- {result['validator_id']}: {result['status']}"
        for result in check_results
    ]
    return "\n".join(
        [
            "Workspace Governance Control Fabric Check",
            f"plan: {plan['plan_id']}",
            f"target: {receipt['target_scope']}",
            f"outcome: {receipt['outcome']}",
            f"receipt: {receipt['receipt_id']}",
            f"receipt path: {record['receipt_path']}",
            f"ledger path: {record['ledger_path']}",
            f"artifact root: {record['artifact_root']}",
            "check results:",
            *(result_lines or ["- none"]),
            "raw output: suppressed into receipt-linked artifacts",
        ],
    )


def render_receipts_list_human(record: dict[str, object]) -> str:
    receipts = record["receipts"]
    assert isinstance(receipts, list)
    receipt_lines = [
        (
            f"- {receipt['receipt_id']} {receipt['outcome']} "
            f"{receipt['target_scope']} {receipt['captured_at']}"
        )
        for receipt in receipts
    ]
    return "\n".join(
        [
            "Workspace Governance Control Fabric Receipts",
            f"receipt dir: {record['receipt_dir']}",
            f"count: {record['count']}",
            *(receipt_lines or ["- none"]),
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

    if args.command == "plan":
        manifest_path = _resolve_manifest_path(args.repo_root, args.manifest)
        plan = build_operator_validation_plan(manifest_path, args.scope, tier=args.tier)
        record = {
            "manifest_path": str(manifest_path.relative_to(Path(args.repo_root).resolve())),
            "plan": plan.to_record(),
        }
        if args.json:
            print(json.dumps(record, indent=2, sort_keys=True))
        else:
            print(render_validation_plan_human(record))
        return 0 if plan.decision.outcome == "planned" else 1

    if args.command == "check":
        repo_root = Path(args.repo_root).resolve()
        manifest_path = _resolve_manifest_path(args.repo_root, args.manifest)
        result = run_operator_validation_check(
            actor=args.actor,
            artifact_root=_resolve_repo_local_path(repo_root, args.artifact_root, "artifact-root"),
            ledger_path=_resolve_repo_local_path(repo_root, args.ledger, "ledger"),
            manifest_path=manifest_path,
            receipt_dir=_resolve_repo_local_path(repo_root, args.receipt_dir, "receipt-dir"),
            repo_root=repo_root,
            target_scope=args.scope,
            tier=args.tier,
        )
        record = result.to_record()
        if args.json:
            print(json.dumps(record, indent=2, sort_keys=True))
        else:
            print(render_check_human(record))
        return 0 if result.receipt.outcome == "success" else 1

    if args.command == "receipts" and args.receipts_command == "list":
        repo_root = Path(args.repo_root).resolve()
        receipt_dir = _resolve_repo_local_path(repo_root, args.receipt_dir, "receipt-dir")
        receipts = [receipt.to_record() for receipt in list_control_receipts(receipt_dir)]
        record = {
            "count": len(receipts),
            "receipt_dir": str(receipt_dir.relative_to(repo_root)),
            "receipts": receipts,
        }
        if args.json:
            print(json.dumps(record, indent=2, sort_keys=True))
        else:
            print(render_receipts_list_human(record))
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


def _resolve_repo_local_path(repo_root: Path, value: str, label: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(repo_root):
        raise ValueError(f"{label} path must stay inside the repository root")
    return resolved


def _add_repo_manifest_scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve repo-local paths.",
    )
    parser.add_argument(
        "--manifest",
        default=DEFAULT_MANIFEST_PATH,
        help="Repo-local governance manifest path.",
    )
    parser.add_argument(
        "--scope",
        required=True,
        help="Validation scope such as repo:<id>, component:<id>, art:<id>, or workspace.",
    )
