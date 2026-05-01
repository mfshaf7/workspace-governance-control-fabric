from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import (
    build_operator_validation_plan,
    evaluate_operator_readiness,
    inspect_control_receipt,
    list_control_receipts,
    run_operator_readiness_evaluation,
    run_operator_validation_check,
)


def minimal_manifest(command: str) -> dict:
    return {
        "schema_version": 1,
        "manifest_id": "wgcf-operator-surface-test",
        "owner_repo": "workspace-governance-control-fabric",
        "authority_refs": [
            {
                "authority_id": "wgcf-runtime-repo-guidance",
                "repo": "workspace-governance-control-fabric",
                "path": "AGENTS.md",
                "ref": "main",
                "digest": "sha256:example",
                "freshness_status": "current",
            },
        ],
        "repos": [
            {
                "repo_id": "workspace-governance-control-fabric",
                "owner_repo": "workspace-governance-control-fabric",
                "authority_ref_ids": ["wgcf-runtime-repo-guidance"],
            },
        ],
        "components": [],
        "validators": [
            {
                "validator_id": "wgcf-operator-surface-smoke",
                "owner_repo": "workspace-governance-control-fabric",
                "command": command,
                "scopes": ["repo:workspace-governance-control-fabric"],
                "validation_tier": "smoke",
                "check_type": "command",
                "required": True,
                "authority_ref_ids": ["wgcf-runtime-repo-guidance"],
            },
        ],
        "projections": [],
    }


class OperatorSurfaceTests(TestCase):
    def test_build_operator_validation_plan_returns_compact_record(self) -> None:
        manifest_path = REPO_ROOT / "examples/governance-manifest.example.json"

        plan = build_operator_validation_plan(
            manifest_path,
            "repo:workspace-governance-control-fabric",
            tier="smoke",
        )

        self.assertEqual(plan.decision.outcome, "planned")
        self.assertEqual(plan.target.scope, "repo:workspace-governance-control-fabric")
        self.assertEqual(plan.checks[0].validator_id, "control-fabric-status-smoke")

    def test_run_operator_check_writes_receipt_and_lists_summary(self) -> None:
        marker = "OPERATOR-SURFACE-RAW-OUTPUT"
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            manifest_path = temp_path / "manifest.json"
            manifest_path.write_text(
                json.dumps(minimal_manifest(f"python3 -c \"print('{marker}')\"")),
                encoding="utf-8",
            )

            result = run_operator_validation_check(
                actor="test-operator",
                artifact_root=temp_path / "artifacts",
                ledger_path=temp_path / "ledger.jsonl",
                manifest_path=manifest_path,
                receipt_dir=temp_path / "receipts",
                repo_root=REPO_ROOT,
                target_scope="repo:workspace-governance-control-fabric",
                tier="smoke",
            )

            receipt_record = result.receipt.to_record()
            receipt_path = Path(result.receipt_path)
            ledger_path = Path(result.ledger_path)
            self.assertEqual(result.receipt.outcome, "success")
            self.assertTrue(receipt_path.is_file())
            self.assertTrue(ledger_path.is_file())
            self.assertNotIn(marker, json.dumps(receipt_record, sort_keys=True))
            self.assertIn(
                marker,
                Path(result.receipt.artifact_refs[0].path).read_text(encoding="utf-8"),
            )

            summaries = list_control_receipts(temp_path / "receipts")
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].receipt_id, result.receipt.receipt_id)
            self.assertEqual(summaries[0].artifact_count, 2)

            inspection = inspect_control_receipt(
                result.receipt.receipt_id,
                receipt_dir=temp_path / "receipts",
            )
            inspection_record = inspection.to_record()
            self.assertEqual(inspection_record["receipt"]["receipt_id"], result.receipt.receipt_id)
            self.assertFalse(inspection_record["raw_output_embedded"])
            self.assertNotIn(marker, json.dumps(inspection_record, sort_keys=True))

            outside_receipt = temp_path / "outside-receipt.json"
            outside_receipt.write_text(json.dumps(result.receipt.to_record()), encoding="utf-8")
            with self.assertRaises(ValueError):
                inspect_control_receipt(outside_receipt, receipt_dir=temp_path / "receipts")

    def test_operator_readiness_decision_is_compact_and_read_only(self) -> None:
        decision = evaluate_operator_readiness(
            profile="local-read-only",
            repo_root=REPO_ROOT,
            target="repo:workspace-governance-control-fabric",
        )
        record = decision.to_record()

        self.assertTrue(record["ready"])
        self.assertEqual(record["outcome"], "ready")
        self.assertTrue(record["correlation_id"].startswith("correlation:readiness:"))
        self.assertTrue(record["metrics"]["ready"])
        self.assertEqual(record["mutation_boundary"], "fabric-local decision record only")
        self.assertIn(
            "workspace-governance/contracts/governance-control-fabric-operator-surface.yaml",
            record["authority_refs"],
        )

    def test_operator_readiness_blocks_unknown_authority_or_profile(self) -> None:
        unknown_target = evaluate_operator_readiness(
            profile="local-read-only",
            repo_root=REPO_ROOT,
            target="repo:not-a-governed-repo",
        ).to_record()
        unknown_profile = evaluate_operator_readiness(
            profile="ad-hoc",
            repo_root=REPO_ROOT,
            target="repo:workspace-governance-control-fabric",
        ).to_record()

        self.assertFalse(unknown_target["ready"])
        self.assertEqual(unknown_target["outcome"], "blocked")
        self.assertFalse(unknown_target["metrics"]["ready"])
        self.assertGreater(unknown_target["metrics"]["blocked_reason_count"], 0)
        self.assertIn("unknown repo target: not-a-governed-repo", unknown_target["reasons"])
        self.assertFalse(unknown_profile["ready"])
        self.assertIn(
            "unknown profile: ad-hoc; expected one of local-read-only, dev-integration, governed-stage",
            unknown_profile["reasons"],
        )

    def test_operator_readiness_appends_local_ledger_event(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            result = run_operator_readiness_evaluation(
                actor="test-operator",
                ledger_path=temp_path / "ledger.jsonl",
                now="2026-05-01T00:00:00Z",
                profile="local-read-only",
                receipt_dir=temp_path / "receipts",
                repo_root=REPO_ROOT,
                target="operator-surface:wgcf-cli",
            )
            ledger_record = json.loads((temp_path / "ledger.jsonl").read_text(encoding="utf-8"))

        self.assertTrue(result.decision.ready)
        self.assertEqual(result.ledger_event.action, "readiness.decision.recorded")
        self.assertEqual(result.ledger_event.outcome, "success")
        self.assertEqual(ledger_record["action"], "readiness.decision.recorded")
