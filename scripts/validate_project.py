#!/usr/bin/env python3
"""Validate the bootstrap Python project scaffold."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path


REQUIRED_PATHS = (
    "pyproject.toml",
    "Dockerfile",
    "alembic.ini",
    "scripts/validate_project.py",
    ".github/workflows/build-image.yaml",
    "apps/api/README.md",
    "apps/api/src/wgcf_api/app.py",
    "apps/cli/README.md",
    "apps/cli/src/wgcf_cli/main.py",
    "apps/worker/README.md",
    "apps/worker/src/wgcf_worker/__init__.py",
    "apps/worker/src/wgcf_worker/__main__.py",
    "apps/worker/src/wgcf_worker/main.py",
    "packages/control_fabric_core/README.md",
    "packages/control_fabric_core/src/control_fabric_core/art_readiness.py",
    "packages/control_fabric_core/src/control_fabric_core/database.py",
    "packages/control_fabric_core/src/control_fabric_core/db/models.py",
    "packages/control_fabric_core/src/control_fabric_core/evidence_projection.py",
    "packages/control_fabric_core/src/control_fabric_core/foundation.py",
    "packages/control_fabric_core/src/control_fabric_core/graph_ingestion.py",
    "packages/control_fabric_core/src/control_fabric_core/graph_persistence.py",
    "packages/control_fabric_core/src/control_fabric_core/graph_queries.py",
    "packages/control_fabric_core/src/control_fabric_core/lifecycle.py",
    "packages/control_fabric_core/src/control_fabric_core/manifests.py",
    "packages/control_fabric_core/src/control_fabric_core/operator_surfaces.py",
    "packages/control_fabric_core/src/control_fabric_core/performance_budgets.py",
    "packages/control_fabric_core/src/control_fabric_core/policy_admission.py",
    "packages/control_fabric_core/src/control_fabric_core/runtime_governance_records.py",
    "packages/control_fabric_core/src/control_fabric_core/source_snapshots.py",
    "packages/control_fabric_core/src/control_fabric_core/validation_execution.py",
    "packages/control_fabric_core/src/control_fabric_core/validation_planning.py",
    "packages/control_fabric_core/src/control_fabric_core/worker.py",
    "docs/architecture/project-structure.md",
    "docs/architecture/governance-operations-console-readiness.md",
    "docs/architecture/context-governance-gateway-integration.md",
    "docs/operations/operator-surface.md",
    "dev-integration/profiles/governance-control-fabric/profile.yaml",
    "dev-integration/profiles/governance-control-fabric/scripts/access.sh",
    "dev-integration/profiles/governance-control-fabric/scripts/common.sh",
    "dev-integration/profiles/governance-control-fabric/scripts/down.sh",
    "dev-integration/profiles/governance-control-fabric/scripts/promote-check.sh",
    "dev-integration/profiles/governance-control-fabric/scripts/reset.sh",
    "dev-integration/profiles/governance-control-fabric/scripts/smoke.sh",
    "dev-integration/profiles/governance-control-fabric/scripts/status.sh",
    "dev-integration/profiles/governance-control-fabric/scripts/up.sh",
    "examples/governance-manifest.example.json",
    "migrations/env.py",
    "migrations/versions/0001_create_foundation_tables.py",
    "policies/opa/admission.rego",
    "policies/opa/policy_ledger.rego",
    "policies/opa/validation_blocking.rego",
    "schemas/art-evidence-packet.schema.json",
    "schemas/art-readiness-receipt.schema.json",
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
        build_art_runtime_graph,
        build_manifest_graph,
        build_operator_validation_plan,
        build_retention_plan,
        build_source_snapshot,
        bootstrap_validation_contract,
        build_policy_ledger_event,
        build_validation_plan,
        evaluate_operation_budget,
        evaluate_admission_policy,
        evaluate_art_readiness,
        evaluate_operator_readiness,
        execute_validation_plan,
        governance_manifest_schema,
        inspect_control_receipt,
        list_control_receipts,
        persist_governance_state,
        resolve_performance_budget,
        project_receipt_to_art_completion_evidence,
        project_receipts_to_art_evidence_packet,
        project_receipt_to_change_record_references,
        project_receipt_to_review_packet_evidence,
        query_manifest_graph,
        record_blocker_decision,
        record_change_event,
        apply_retention_plan,
        run_operator_readiness_evaluation,
        run_operator_validation_check,
        status_snapshot,
        validate_governance_manifest,
        worker_status_snapshot,
    )
    from control_fabric_core.db import metadata
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
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
    bootstrap_contract = bootstrap_validation_contract(repo_root)
    if bootstrap_contract["uses_wgcf_receipt_as_bootstrap_authority"]:
        errors.append("bootstrap validation must not use WGCF receipts as bootstrap authority")
    if not bootstrap_contract["bootstrap_validator_present"]:
        errors.append("bootstrap validator script is missing")
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
    sources_parsed = parser.parse_args(["sources", "snapshot", "--repo-root", str(repo_root)])
    if sources_parsed.command != "sources" or sources_parsed.sources_command != "snapshot":
        errors.append("wgcf parser did not accept sources snapshot command")
    receipts_parsed = parser.parse_args(["receipts", "list", "--repo-root", str(repo_root)])
    if receipts_parsed.command != "receipts" or receipts_parsed.receipts_command != "list":
        errors.append("wgcf parser did not accept receipts list command")
    budget_parsed = parser.parse_args(["budget", "show", "--operation", "art.continuation"])
    if budget_parsed.command != "budget" or budget_parsed.budget_command != "show":
        errors.append("wgcf parser did not accept budget show command")
    lifecycle_parsed = parser.parse_args(["lifecycle", "plan", "--repo-root", str(repo_root)])
    if lifecycle_parsed.command != "lifecycle" or lifecycle_parsed.lifecycle_command != "plan":
        errors.append("wgcf parser did not accept lifecycle plan command")
    inspect_parsed = parser.parse_args(["inspect", "--receipt", "control-receipt:example"])
    if inspect_parsed.command != "inspect":
        errors.append("wgcf parser did not accept inspect command")
    readiness_parsed = parser.parse_args(
        [
            "readiness",
            "--target",
            "operator-surface:wgcf-cli",
            "--profile",
            "local-read-only",
        ],
    )
    if readiness_parsed.command != "readiness":
        errors.append("wgcf parser did not accept readiness command")
    art_readiness_parsed = parser.parse_args(
        ["art", "readiness", "--context", "context.json", "--target-item-id", "517"],
    )
    if art_readiness_parsed.command != "art" or art_readiness_parsed.art_command != "readiness":
        errors.append("wgcf parser did not accept art readiness command")
    worker_parser = build_worker_parser()
    worker_parsed = worker_parser.parse_args(["status", "--repo-root", str(repo_root)])
    if worker_parsed.command != "status":
        errors.append("wgcf-worker parser did not accept status command")
    app = create_app(repo_root)
    if app.title != "Workspace Governance Control Fabric":
        errors.append("FastAPI app title is not the control-fabric title")
    route_paths = {route.path for route in app.routes}
    for required_route in (
        "/v1/validation-runs",
        "/v1/receipts/{receipt_id}",
        "/v1/readiness/evaluate",
        "/v1/budgets",
        "/v1/lifecycle/retention-plan",
        "/v1/lifecycle/retention-apply",
    ):
        if required_route not in route_paths:
            errors.append(f"FastAPI route missing: {required_route}")
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
    continuation_budget = resolve_performance_budget("art.continuation")
    submit_budget = resolve_performance_budget("draft.submit")
    unknown_budget = evaluate_operation_budget("future.unclassified.operation")
    if continuation_budget.invocation_class != "inline-fast":
        errors.append("ART continuation budget must be inline-fast")
    if submit_budget.invocation_class != "receipt-check":
        errors.append("draft submit budget must be receipt-check")
    if unknown_budget.within_budget:
        errors.append("unknown WGCF operation budget must require classification")
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
    scaffold_validator = next(
        (
            validator
            for validator in example_manifest["validators"]
            if validator["validator_id"] == "control-fabric-project-scaffold"
        ),
        None,
    )
    if scaffold_validator is None:
        errors.append("example manifest missing control-fabric-project-scaffold validator")
    else:
        scaffold_command = str(scaffold_validator.get("command") or "")
        scaffold_policy = scaffold_validator.get("execution_policy") or {}
        if scaffold_policy.get("self_validation_role") != "bootstrap-independent":
            errors.append("project scaffold validator must declare bootstrap-independent self-validation")
        if "scripts/validate_project.py" not in scaffold_command:
            errors.append("project scaffold validator must call the direct bootstrap validator script")
        for forbidden_token in bootstrap_contract["forbidden_runtime_entrypoints"]:
            if forbidden_token in scaffold_command:
                errors.append(
                    f"project scaffold validator must not invoke recursive WGCF entrypoint: {forbidden_token}",
                )
    operator_plan = build_operator_validation_plan(
        example_path,
        "repo:workspace-governance-control-fabric",
        tier="smoke",
    )
    if operator_plan.decision.outcome != "planned":
        errors.append("operator validation plan was not planned")
    source_snapshot = build_source_snapshot(repo_root.parent, actor="wgcf-validate-project")
    source_record = source_snapshot.to_record()
    authority_repos = {
        source_ref["repo"]
        for source_ref in source_record["authority_refs"]
    }
    owner_map_present = (
        repo_root.parent / "workspace-governance/generated/resolved-owner-map.json"
    ).is_file()
    if owner_map_present:
        for required_authority_repo in (
            "workspace-governance",
            "platform-engineering",
            "security-architecture",
            "operator-orchestration-service",
            "workspace-governance-control-fabric",
        ):
            if required_authority_repo not in authority_repos:
                errors.append(f"source snapshot missing authority repo: {required_authority_repo}")
    elif "workspace-governance-control-fabric" not in authority_repos:
        errors.append("source snapshot missing local runtime repo authority refs")
    authority_kinds = {
        source_ref["source_kind"]
        for source_ref in source_record["authority_refs"]
    }
    required_source_kinds = (
        "workspace-authority",
        "validator-catalog",
        "platform-runtime",
        "security-review",
        "operator-workflow",
        "repo-manifest",
        "dev-integration-profile",
    )
    if not owner_map_present:
        required_source_kinds = ("repo-manifest", "dev-integration-profile")
        if source_record["summary"]["excluded_ref_count"] == 0:
            errors.append("isolated source snapshot should report excluded upstream authority refs")
    for required_kind in required_source_kinds:
        if required_kind not in authority_kinds:
            errors.append(f"source snapshot missing source kind: {required_kind}")
    minimum_authority_refs = 20 if owner_map_present else 3
    if source_record["summary"]["authority_ref_count"] < minimum_authority_refs:
        errors.append("source snapshot returned too few authority refs for workspace ingestion")
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        persistence_result = persist_governance_state(
            session,
            graph=graph,
            snapshot=source_snapshot,
        )
        session.commit()
    if persistence_result.source_snapshot.authority_ref_count != source_record["summary"]["authority_ref_count"]:
        errors.append("source snapshot persistence result did not match source snapshot")
    if persistence_result.graph.node_count < len(graph.nodes):
        errors.append("graph persistence result returned fewer nodes than the manifest graph")
    if persistence_result.graph.edge_count != len(graph.edges):
        errors.append("graph persistence result did not match manifest graph edges")
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
        inspection = inspect_control_receipt(
            operator_result.receipt.receipt_id,
            receipt_dir=temp_path / "receipts",
        )
        if inspection.raw_output_embedded:
            errors.append("receipt inspection must not embed raw output")
        if inspection.receipt.get("receipt_id") != operator_result.receipt.receipt_id:
            errors.append("receipt inspection did not resolve the requested receipt id")
        readiness_decision = evaluate_operator_readiness(
            profile="local-read-only",
            receipt_dir=temp_path / "receipts",
            repo_root=repo_root,
            target="operator-surface:wgcf-cli",
        )
        if not readiness_decision.ready:
            errors.append("operator readiness unexpectedly blocked the CLI surface")
        readiness_result = run_operator_readiness_evaluation(
            actor="wgcf-project-validator",
            ledger_path=temp_path / "readiness-ledger.jsonl",
            now="2026-04-30T00:00:00Z",
            profile="local-read-only",
            receipt_dir=temp_path / "receipts",
            repo_root=repo_root,
            target="operator-surface:wgcf-cli",
        )
        if readiness_result.ledger_event.action != "readiness.decision.recorded":
            errors.append("operator readiness did not emit the readiness ledger action")
        if not Path(readiness_result.ledger_path).is_file():
            errors.append("operator readiness did not append a ledger event")
        old_artifact = temp_path / "lifecycle/artifacts/old.txt"
        old_artifact.parent.mkdir(parents=True, exist_ok=True)
        old_artifact.write_text("old artifact", encoding="utf-8")
        os.utime(old_artifact, (0, 0))
        lifecycle_plan = build_retention_plan(
            artifact_root=temp_path / "lifecycle/artifacts",
            ledger_path=temp_path / "lifecycle-ledger.jsonl",
            now="2026-04-30T00:00:00Z",
            profile="developer",
            receipt_dir=temp_path / "lifecycle/receipts",
            repo_root=repo_root,
        )
        if lifecycle_plan.summary["artifact_delete_count"] != 1:
            errors.append("lifecycle retention plan did not identify the old artifact")
        blocked_lifecycle = apply_retention_plan(
            artifact_root=temp_path / "lifecycle/artifacts",
            confirm=False,
            ledger_path=temp_path / "lifecycle-ledger.jsonl",
            now="2026-04-30T00:00:00Z",
            receipt_dir=temp_path / "lifecycle/receipts",
            repo_root=repo_root,
        )
        if blocked_lifecycle.outcome != "blocked":
            errors.append("lifecycle retention apply must block without confirmation")
        lifecycle_result = apply_retention_plan(
            actor="wgcf-project-validator",
            artifact_root=temp_path / "lifecycle/artifacts",
            confirm=True,
            ledger_path=temp_path / "lifecycle-ledger.jsonl",
            now="2026-04-30T00:00:00Z",
            receipt_dir=temp_path / "lifecycle/receipts",
            repo_root=repo_root,
        )
        if lifecycle_result.outcome != "success":
            errors.append("lifecycle retention apply did not succeed with confirmation")
        if old_artifact.exists():
            errors.append("lifecycle retention apply did not remove the old artifact")
        if lifecycle_result.ledger_event is None or lifecycle_result.ledger_event["action"] != "lifecycle.retention.applied":
            errors.append("lifecycle retention apply did not append the lifecycle ledger action")
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
    art_context = {
        "continuation_context": {
            "summary": {"open_child_count": 1},
            "target_item": {
                "descriptionHeadings": [
                    "What This Enables",
                    "Benefit Hypothesis",
                    "Scope Boundaries",
                    "Evidence Expectation",
                    "Execution Context",
                    "Operator work notes",
                ],
                "descriptionPresent": True,
                "delivery_team": "Platform Architecture",
                "id": 517,
                "iteration": "PI-2026-03 / Iteration 1",
                "owner_repo": "workspace-governance-control-fabric",
                "status": "ready",
                "target_pi": "PI-2026-03",
                "type": "Feature",
            },
        },
        "projection_state": {"dirty": False},
    }
    art_graph = build_art_runtime_graph(art_context, now="2026-04-30T00:00:00Z")
    if art_graph.summary["node_count"] == 0:
        errors.append("ART runtime graph did not ingest broker context")
    art_readiness = evaluate_art_readiness(
        art_context,
        operation="complete",
        target_item_id=517,
        now="2026-04-30T00:00:00Z",
    )
    if not art_readiness.mutation_allowed:
        errors.append("ART readiness unexpectedly blocked clean synthetic context")
    art_evidence_packet = project_receipts_to_art_evidence_packet(
        [receipt_record],
        changed_surfaces=[
            "`packages/control_fabric_core/src/control_fabric_core/art_readiness.py`: validates ART evidence packet projection.",
        ],
        completion_summary="Synthetic ART evidence packet generated.",
        item_ids=[517],
        now="2026-04-30T00:00:00Z",
    )
    if art_evidence_packet.raw_artifacts_embedded:
        errors.append("ART evidence packet must not embed raw artifacts")
    if "- PASS:" not in art_evidence_packet.completion_payload["validation_evidence"]:
        errors.append("ART evidence packet validation evidence is not completion-preflight compatible")
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
