#!/usr/bin/env python3
"""Validate the bootstrap Python project scaffold."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
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
    "apps/worker/src/wgcf_worker/__init__.py",
    "apps/worker/src/wgcf_worker/__main__.py",
    "apps/worker/src/wgcf_worker/main.py",
    "packages/control_fabric_core/README.md",
    "packages/control_fabric_core/src/control_fabric_core/database.py",
    "packages/control_fabric_core/src/control_fabric_core/db/models.py",
    "packages/control_fabric_core/src/control_fabric_core/evidence_projection.py",
    "packages/control_fabric_core/src/control_fabric_core/foundation.py",
    "packages/control_fabric_core/src/control_fabric_core/graph_ingestion.py",
    "packages/control_fabric_core/src/control_fabric_core/graph_queries.py",
    "packages/control_fabric_core/src/control_fabric_core/manifests.py",
    "packages/control_fabric_core/src/control_fabric_core/operator_surfaces.py",
    "packages/control_fabric_core/src/control_fabric_core/policy_admission.py",
    "packages/control_fabric_core/src/control_fabric_core/runtime_governance_records.py",
    "packages/control_fabric_core/src/control_fabric_core/validation_execution.py",
    "packages/control_fabric_core/src/control_fabric_core/validation_planning.py",
    "packages/control_fabric_core/src/control_fabric_core/worker.py",
    "docs/architecture/project-structure.md",
    "docs/architecture/governance-operations-console-readiness.md",
    "docs/architecture/context-governance-gateway-integration.md",
    "docs/operations/operator-surface.md",
    "examples/governance-manifest.example.json",
    "migrations/env.py",
    "migrations/versions/0001_create_foundation_tables.py",
    "policies/opa/admission.rego",
    "policies/opa/policy_ledger.rego",
    "policies/opa/validation_blocking.rego",
    "schemas/evidence-projection.schema.json",
    "schemas/governance-manifest.schema.json",
    "schemas/ledger-event.schema.json",
    "schemas/policy-decision.schema.json",
    "schemas/runtime-governance-record.schema.json",
    "schemas/validation-receipt.schema.json",
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
    if scripts.get("wgcf-worker") != "wgcf_worker.main:main":
        errors.append("pyproject must expose wgcf-worker = wgcf_worker.main:main")
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
    sys.path.insert(0, str(repo_root / "apps/worker/src"))

    from control_fabric_core import (
        build_governance_record_ledger_event,
        build_manifest_graph,
        build_operator_validation_plan,
        build_policy_ledger_event,
        build_validation_plan,
        evaluate_admission_policy,
        execute_validation_plan,
        governance_manifest_schema,
        list_control_receipts,
        project_receipt_to_art_completion_evidence,
        project_receipt_to_change_record_references,
        project_receipt_to_review_packet_evidence,
        query_manifest_graph,
        record_blocker_decision,
        record_change_event,
        run_operator_validation_check,
        status_snapshot,
        validate_governance_manifest,
        worker_status_snapshot,
    )
    from control_fabric_core.db import metadata
    from wgcf_api import create_app
    from wgcf_cli.main import build_parser
    from wgcf_worker.main import build_parser as build_worker_parser

    snapshot = status_snapshot(repo_root)
    worker_snapshot = worker_status_snapshot(repo_root)
    errors: list[str] = []
    if not snapshot["ready"]:
        errors.append("status snapshot is not ready")
    if not worker_snapshot["ready"]:
        errors.append("worker status snapshot is not ready")
    parser = build_parser()
    parsed = parser.parse_args(["status", "--repo-root", str(repo_root)])
    if parsed.command != "status":
        errors.append("wgcf parser did not accept status command")
    plan_parsed = parser.parse_args(
        [
            "plan",
            "--repo-root",
            str(repo_root),
            "--scope",
            "repo:workspace-governance-control-fabric",
        ],
    )
    if plan_parsed.command != "plan":
        errors.append("wgcf parser did not accept plan command")
    check_parsed = parser.parse_args(
        [
            "check",
            "--repo-root",
            str(repo_root),
            "--scope",
            "repo:workspace-governance-control-fabric",
        ],
    )
    if check_parsed.command != "check":
        errors.append("wgcf parser did not accept check command")
    receipts_parsed = parser.parse_args(["receipts", "list", "--repo-root", str(repo_root)])
    if receipts_parsed.command != "receipts" or receipts_parsed.receipts_command != "list":
        errors.append("wgcf parser did not accept receipts list command")
    worker_parser = build_worker_parser()
    worker_parsed = worker_parser.parse_args(["status", "--repo-root", str(repo_root)])
    if worker_parsed.command != "status":
        errors.append("wgcf-worker parser did not accept status command")
    app = create_app(repo_root)
    if app.title != "Workspace Governance Control Fabric":
        errors.append("FastAPI app title is not the control-fabric title")
    missing_tables = REQUIRED_DB_TABLES.difference(metadata.tables)
    if missing_tables:
        errors.append(f"database metadata missing required tables: {sorted(missing_tables)}")
    capability_ids = {
        capability["capability_id"]
        for capability in worker_snapshot["capabilities"]
    }
    for required_capability in (
        "source-snapshot-ingest",
        "validation-plan-execute",
        "control-receipt-append",
    ):
        if required_capability not in capability_ids:
            errors.append(f"worker capability missing: {required_capability}")
    if worker_snapshot["temporal"]["connects_to_temporal"]:
        errors.append("worker scaffold must not connect to Temporal yet")
    if worker_snapshot["temporal"]["long_running_worker"]:
        errors.append("worker scaffold must not run as a long-running worker yet")
    schema_path = repo_root / "schemas/governance-manifest.schema.json"
    example_path = repo_root / "examples/governance-manifest.example.json"
    static_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    if static_schema != governance_manifest_schema():
        errors.append("static governance manifest schema does not match runtime schema")
    example_manifest = json.loads(example_path.read_text(encoding="utf-8"))
    manifest_result = validate_governance_manifest(example_manifest)
    if not manifest_result.valid:
        errors.append(f"example governance manifest invalid: {list(manifest_result.errors)}")
    graph = build_manifest_graph(example_manifest)
    node_types = {node.node_type for node in graph.nodes}
    for required_node_type in ("authority-reference", "repo", "component", "validator", "projection"):
        if required_node_type not in node_types:
            errors.append(f"example governance manifest graph missing node type: {required_node_type}")
    repo_query = query_manifest_graph(example_manifest, "repo:workspace-governance-control-fabric")
    if not repo_query.nodes:
        errors.append("example governance manifest repo query returned no nodes")
    art_query = query_manifest_graph(example_manifest, "art:delivery-420")
    if not any(node.node_type == "scope" for node in art_query.nodes):
        errors.append("example governance manifest ART query missing synthetic scope node")
    validation_plan = build_validation_plan(
        example_manifest,
        "repo:workspace-governance-control-fabric",
        tier="scoped",
    )
    if validation_plan.decision.outcome != "planned":
        errors.append("example governance manifest validation plan was not planned")
    if not validation_plan.checks:
        errors.append("example governance manifest validation plan returned no checks")
    operator_plan = build_operator_validation_plan(
        example_path,
        "repo:workspace-governance-control-fabric",
        tier="smoke",
    )
    if operator_plan.decision.outcome != "planned":
        errors.append("operator validation plan was not planned")
    synthetic_manifest = json.loads(json.dumps(example_manifest))
    synthetic_manifest["manifest_id"] = "wgcf-validation-project-self-check"
    synthetic_manifest["validators"] = [
        {
            "validator_id": "wgcf-validation-execution-self-check",
            "owner_repo": "workspace-governance-control-fabric",
            "command": "python3 -c \"print('wgcf receipt ok')\"",
            "scopes": ["repo:workspace-governance-control-fabric"],
            "validation_tier": "smoke",
            "check_type": "command",
            "required": True,
            "authority_ref_ids": ["wgcf-runtime-repo-guidance"],
        },
    ]
    synthetic_plan = build_validation_plan(
        synthetic_manifest,
        "repo:workspace-governance-control-fabric",
        tier="smoke",
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        execution_result = execute_validation_plan(
            synthetic_plan,
            repo_root,
            Path(temp_dir),
            now="2026-04-30T00:00:00Z",
            timeout_seconds=120,
        )
    with tempfile.TemporaryDirectory(dir=repo_root) as temp_dir:
        temp_path = Path(temp_dir)
        synthetic_manifest_path = temp_path / "manifest.json"
        synthetic_manifest_path.write_text(
            json.dumps(synthetic_manifest),
            encoding="utf-8",
        )
        operator_result = run_operator_validation_check(
            actor="wgcf-project-validator",
            artifact_root=temp_path / "artifacts",
            ledger_path=temp_path / "ledger.jsonl",
            manifest_path=synthetic_manifest_path,
            receipt_dir=temp_path / "receipts",
            repo_root=repo_root,
            target_scope="repo:workspace-governance-control-fabric",
            tier="smoke",
        )
        receipt_summaries = list_control_receipts(temp_path / "receipts")
        if operator_result.receipt.outcome != "success":
            errors.append("operator check did not produce a successful receipt")
        if len(receipt_summaries) != 1:
            errors.append("operator receipt listing did not find the written receipt")
    receipt_record = execution_result.receipt.to_record()
    if receipt_record["suppressed_output_summary"]["raw_output_in_receipt"]:
        errors.append("validation receipt must not embed raw validator output")
    if execution_result.ledger_event.target != "repo:workspace-governance-control-fabric":
        errors.append("validation ledger event target did not match plan target")
    policy_decision = evaluate_admission_policy(
        {
            "authority_refs": [
                {
                    "authority_id": "wgcf-runtime-repo-guidance",
                    "digest": "sha256:example",
                    "freshness_status": "current",
                },
            ],
            "owner_repo": "workspace-governance-control-fabric",
            "receipt_refs": [
                {
                    "digest": execution_result.receipt.digest,
                    "outcome": execution_result.receipt.outcome,
                    "receipt_id": execution_result.receipt.receipt_id,
                },
            ],
            "subject_id": "workspace-governance-control-fabric",
            "subject_type": "repo",
        },
        now="2026-04-30T00:00:00Z",
    )
    if policy_decision.outcome != "allow":
        errors.append("synthetic policy admission decision was not allow")
    policy_event = build_policy_ledger_event(
        actor="wgcf-project-validator",
        decision=policy_decision,
    )
    if policy_event.action != "policy.decision.recorded":
        errors.append("policy ledger event action was not recorded")
    art_projection = project_receipt_to_art_completion_evidence(
        execution_result.receipt,
        changed_surfaces=[
            "`scripts/validate_project.py`: validates compact receipt projection adapters.",
        ],
        policy_decisions=[policy_decision],
        now="2026-04-30T00:00:00Z",
    )
    if art_projection.projection.raw_artifacts_embedded:
        errors.append("ART evidence projection must not embed raw artifacts")
    if not art_projection.to_completion_payload()["validation_evidence"]:
        errors.append("ART evidence projection did not render validation evidence")
    review_projection = project_receipt_to_review_packet_evidence(
        execution_result.receipt,
        changed_surface_explanations=[
            "`packages/control_fabric_core`: projects receipt evidence into Review Packets.",
        ],
        item_ids=[451],
        policy_decisions=[policy_decision],
        now="2026-04-30T00:00:00Z",
    )
    if not review_projection.item_evidence_refs:
        errors.append("Review Packet evidence projection missing item evidence refs")
    change_projection = project_receipt_to_change_record_references(
        execution_result.receipt,
        change_record_path="docs/records/change-records/example.md",
        policy_decisions=[policy_decision],
        now="2026-04-30T00:00:00Z",
    )
    if not any(ref.get("evidence_type") == "control_receipt" for ref in change_projection.evidence_refs):
        errors.append("change-record evidence projection missing receipt ref")
    blocker_record = record_blocker_decision(
        blocker_owner="Workspace Governance Control Fabric",
        decision_path="remove",
        impact="project validator synthetic blocker",
        next_required_action="land durable control",
        owner_repo="workspace-governance-control-fabric",
        statement="synthetic blocker record check",
        target="repo:workspace-governance-control-fabric",
        authority_refs=[
            {
                "authority_id": "wgcf-runtime-repo-guidance",
                "digest": "sha256:example",
            },
        ],
        evidence_refs=[
            {
                "digest": execution_result.receipt.digest,
                "outcome": execution_result.receipt.outcome,
                "receipt_id": execution_result.receipt.receipt_id,
            },
        ],
        now="2026-04-30T00:00:00Z",
    )
    blocker_event = build_governance_record_ledger_event(
        actor="wgcf-project-validator",
        record=blocker_record,
    )
    if blocker_event.action != "governance.blocker.recorded":
        errors.append("runtime governance blocker ledger event action was not recorded")
    change_record = record_change_event(
        changed_surfaces=["scripts/validate_project.py"],
        evidence_refs=[
            {
                "digest": execution_result.receipt.digest,
                "outcome": execution_result.receipt.outcome,
                "receipt_id": execution_result.receipt.receipt_id,
            },
        ],
        owner_repo="workspace-governance-control-fabric",
        record_ref="docs/records/change-records/example.md",
        target="repo:workspace-governance-control-fabric",
        now="2026-04-30T00:00:00Z",
    )
    if change_record.decision != "recorded":
        errors.append("runtime governance change record was not recorded")
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
