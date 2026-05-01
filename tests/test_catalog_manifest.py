from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import (  # noqa: E402
    build_catalog_governance_manifest,
    build_catalog_operator_validation_plan,
    build_validation_plan,
    execute_validation_plan,
)


def write_authority_files(workspace_root: Path) -> None:
    files = [
        "workspace-governance/contracts/governance-validator-catalog.yaml",
        "workspace-governance/contracts/governance-engine-shadow-parity.yaml",
        "platform-engineering/docs/components/workspace-governance-control-fabric/validator-invocation-gates.md",
        "security-architecture/docs/reviews/components/2026-05-01-wgcf-validator-invocation-and-artifact-custody.md",
    ]
    for rel_path in files:
        path = workspace_root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{rel_path}\n", encoding="utf-8")
    for repo in (
        "operator-orchestration-service",
        "workspace-governance-control-fabric",
    ):
        (workspace_root / repo).mkdir(parents=True, exist_ok=True)


def write_catalog(workspace_root: Path, entries: dict) -> Path:
    catalog_path = workspace_root / "workspace-governance/contracts/governance-validator-catalog.yaml"
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "governance_validator_catalog": {
                    "owner_repo": "workspace-governance",
                    "runtime_repo": "workspace-governance-control-fabric",
                    "representative_scopes": [
                        {
                            "scope_id": "workspace-governance",
                            "planner_scope": "component:workspace-governance",
                            "owner_repo": "workspace-governance",
                            "parity_contract_scope_ref": "workspace-governance",
                            "purpose": "test workspace scope",
                        },
                    ],
                    "entries": entries,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return catalog_path


def catalog_entry(**overrides: object) -> dict:
    payload = {
        "allowed_profiles": ["local-read-only", "dev-integration"],
        "command": "python3 -c \"from pathlib import Path; print('CATALOG-RAW-OUTPUT:' + Path.cwd().name)\"",
        "included_in_validation_matrix": False,
        "kind": "validator",
        "mutates_authority": False,
        "owner_repo": "workspace-governance",
        "purpose": "test validator",
        "requires_network": False,
        "requires_workspace_root": False,
        "retirement_posture": "keep-direct-until-shadow-parity",
        "safety_class": "local-read-only",
        "scope": "repo-local",
        "surface_id": "workspace-governance-python-scripts",
        "wgcf_invocation": {
            "enabled": True,
            "scopes": ["component:workspace-governance", "workspace"],
            "validation_tier": "smoke",
            "working_directory_repo": "workspace-governance",
        },
        "wgcf_posture": "candidate-for-wgcf-invocation",
        "writes_materialized_outputs": False,
    }
    payload.update(overrides)
    return payload


class CatalogManifestTests(TestCase):
    def test_catalog_manifest_selects_invocation_entries_and_suppresses_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            write_authority_files(workspace_root)
            write_catalog(
                workspace_root,
                {
                    "contract-model": catalog_entry(),
                    "placeholder-family": catalog_entry(
                        command="npm run art -- <workflow-health>",
                        owner_repo="operator-orchestration-service",
                        wgcf_invocation=None,
                        wgcf_posture="profile-gated-read-only",
                    ),
                },
            )

            result = build_catalog_governance_manifest(workspace_root=workspace_root)

            self.assertEqual([entry.entry_id for entry in result.selected_entries], ["contract-model"])
            self.assertEqual(result.suppressed_entries[0].entry_id, "placeholder-family")
            self.assertIn("no enabled wgcf_invocation", result.suppressed_entries[0].reason)
            self.assertEqual(result.manifest["validators"][0]["validator_id"], "catalog:contract-model")

    def test_catalog_plan_and_execution_use_declared_working_directory_without_raw_receipt_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            write_authority_files(workspace_root)
            write_catalog(workspace_root, {"contract-model": catalog_entry()})

            plan_result = build_catalog_operator_validation_plan(
                target_scope="component:workspace-governance",
                tier="smoke",
                workspace_root=workspace_root,
            )
            plan = build_validation_plan(
                plan_result.catalog.manifest,
                "component:workspace-governance",
                tier="smoke",
            )

            with tempfile.TemporaryDirectory() as artifact_dir:
                result = execute_validation_plan(
                    plan,
                    repo_root=workspace_root,
                    artifact_root=artifact_dir,
                    now="2026-05-01T00:00:00Z",
                )
                artifact_stdout = Path(result.receipt.artifact_refs[0].path).read_text(encoding="utf-8").strip()

            receipt_record = result.receipt.to_record()
            self.assertEqual(result.receipt.outcome, "success")
            self.assertNotIn("CATALOG-RAW-OUTPUT", json.dumps(receipt_record, sort_keys=True))
            self.assertEqual(artifact_stdout, "CATALOG-RAW-OUTPUT:workspace-governance")

    def test_catalog_operator_record_separates_manifest_and_planned_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            write_authority_files(workspace_root)
            write_catalog(
                workspace_root,
                {
                    "contract-model": catalog_entry(),
                    "security-evidence": catalog_entry(
                        wgcf_invocation={
                            "enabled": True,
                            "scopes": ["component:security-review"],
                            "validation_tier": "smoke",
                            "working_directory_repo": "workspace-governance",
                        },
                    ),
                },
            )

            record = build_catalog_operator_validation_plan(
                target_scope="component:workspace-governance",
                tier="smoke",
                workspace_root=workspace_root,
            ).to_record()

            catalog = record["catalog"]
            self.assertEqual(
                [entry["entry_id"] for entry in catalog["selected_entries"]],
                ["contract-model"],
            )
            self.assertEqual(catalog["selected_entry_count"], 1)
            self.assertEqual(catalog["manifest_selected_entry_count"], 2)
            self.assertEqual(
                sorted(entry["entry_id"] for entry in catalog["manifest_selected_entries"]),
                ["contract-model", "security-evidence"],
            )
            self.assertIn("planner-selected", catalog["selection_note"])
