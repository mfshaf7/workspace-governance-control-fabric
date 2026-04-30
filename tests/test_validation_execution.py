from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import (
    append_ledger_event,
    build_validation_plan,
    execute_validation_plan,
    write_control_receipt,
)


def minimal_manifest(
    command: str,
    *,
    check_type: str = "command",
    required: bool = True,
    reuse_policy: dict | None = None,
    stale_authority: bool = False,
) -> dict:
    validator = {
        "validator_id": "wgcf-test-validator",
        "owner_repo": "workspace-governance-control-fabric",
        "command": command,
        "scopes": ["repo:workspace-governance-control-fabric"],
        "validation_tier": "smoke",
        "check_type": check_type,
        "required": required,
        "authority_ref_ids": ["wgcf-runtime-repo-guidance"],
    }
    if reuse_policy is not None:
        validator["reuse_policy"] = reuse_policy

    return {
        "schema_version": 1,
        "manifest_id": "wgcf-test-manifest",
        "owner_repo": "workspace-governance-control-fabric",
        "authority_refs": [
            {
                "authority_id": "wgcf-runtime-repo-guidance",
                "repo": "workspace-governance-control-fabric",
                "path": "AGENTS.md",
                "ref": "main",
                "digest": "sha256:example",
                "freshness_status": "stale" if stale_authority else "current",
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
        "validators": [validator],
        "projections": [],
    }


class ValidationExecutionTests(TestCase):
    def test_success_command_creates_artifacts_without_raw_receipt_output(self) -> None:
        marker = "RAW-VALIDATOR-OUTPUT-SHOULD-STAY-IN-ARTIFACT"
        plan = build_validation_plan(
            minimal_manifest(f"python3 -c \"print('{marker}')\""),
            "repo:workspace-governance-control-fabric",
            tier="smoke",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = execute_validation_plan(
                plan,
                REPO_ROOT,
                temp_dir,
                now="2026-04-30T00:00:00Z",
            )

            receipt_record = result.receipt.to_record()
            self.assertEqual(result.receipt.outcome, "success")
            self.assertEqual(result.ledger_event.action, "validation.run.completed")
            self.assertEqual(len(result.receipt.artifact_refs), 2)
            self.assertFalse(receipt_record["suppressed_output_summary"]["raw_output_in_receipt"])
            self.assertNotIn(marker, json.dumps(receipt_record, sort_keys=True))
            self.assertIn(
                marker,
                Path(result.receipt.artifact_refs[0].path).read_text(encoding="utf-8"),
            )

    def test_failure_command_records_exit_code_and_artifact_refs(self) -> None:
        plan = build_validation_plan(
            minimal_manifest("python3 -c \"import sys; print('failing'); sys.exit(7)\""),
            "repo:workspace-governance-control-fabric",
            tier="smoke",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = execute_validation_plan(
                plan,
                REPO_ROOT,
                temp_dir,
                now="2026-04-30T00:00:00Z",
            )

        self.assertEqual(result.receipt.outcome, "failure")
        self.assertEqual(result.receipt.check_results[0].status, "failure")
        self.assertEqual(result.receipt.check_results[0].exit_code, 7)
        self.assertEqual(len(result.receipt.check_results[0].artifact_refs), 2)

    def test_env_prefix_command_runs_without_shell(self) -> None:
        plan = build_validation_plan(
            minimal_manifest(
                "WGCF_ENV_TEST=from-prefix python3 -c \"import os; print(os.environ['WGCF_ENV_TEST'])\"",
            ),
            "repo:workspace-governance-control-fabric",
            tier="smoke",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = execute_validation_plan(
                plan,
                REPO_ROOT,
                temp_dir,
                now="2026-04-30T00:00:00Z",
            )
            stdout_path = Path(result.receipt.check_results[0].artifact_refs[0].path)
            stdout_text = stdout_path.read_text(encoding="utf-8").strip()

        self.assertEqual(result.receipt.outcome, "success")
        self.assertEqual(stdout_text, "from-prefix")

    def test_python3_manifest_command_uses_current_interpreter_first(self) -> None:
        plan = build_validation_plan(
            minimal_manifest(
                "python3 -c \"import sys; print(sys.executable)\"",
            ),
            "repo:workspace-governance-control-fabric",
            tier="smoke",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = execute_validation_plan(
                plan,
                REPO_ROOT,
                temp_dir,
                now="2026-04-30T00:00:00Z",
            )
            stdout_path = Path(result.receipt.check_results[0].artifact_refs[0].path)
            stdout_text = stdout_path.read_text(encoding="utf-8").strip()

        self.assertEqual(result.receipt.outcome, "success")
        self.assertEqual(Path(stdout_text).parent, Path(sys.executable).parent)

    def test_fresh_receipt_skip_reuses_without_artifacts(self) -> None:
        manifest = minimal_manifest(
            "python3 -c \"raise SystemExit('should not run')\"",
            reuse_policy={"safe_to_reuse": True, "freshness_seconds": 900},
        )
        plan = build_validation_plan(
            manifest,
            "repo:workspace-governance-control-fabric",
            tier="smoke",
            receipts=[
                {
                    "receipt_id": "receipt:existing-success",
                    "validator_id": "wgcf-test-validator",
                    "target_scope": "repo:workspace-governance-control-fabric",
                    "tier": "smoke",
                    "status": "success",
                    "captured_at": "2026-04-30T00:00:00Z",
                    "digest": "sha256:existing",
                },
            ],
            now="2026-04-30T00:05:00Z",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = execute_validation_plan(
                plan,
                REPO_ROOT,
                temp_dir,
                now="2026-04-30T00:05:00Z",
            )

        self.assertEqual(result.receipt.outcome, "success")
        self.assertEqual(result.receipt.artifact_refs, ())
        self.assertEqual(result.receipt.check_results[0].status, "skipped_fresh_receipt")
        self.assertEqual(result.receipt.check_results[0].reused_receipt_id, "receipt:existing-success")

    def test_blocked_plan_does_not_execute_checks(self) -> None:
        plan = build_validation_plan(
            minimal_manifest(
                "python3 -c \"print('SHOULD-NOT-RUN')\"",
                stale_authority=True,
            ),
            "repo:workspace-governance-control-fabric",
            tier="release",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = execute_validation_plan(
                plan,
                REPO_ROOT,
                temp_dir,
                now="2026-04-30T00:00:00Z",
            )

        self.assertEqual(result.receipt.outcome, "blocked")
        self.assertEqual(result.ledger_event.action, "validation.run.blocked")
        self.assertEqual(result.receipt.check_results, ())
        self.assertEqual(result.receipt.artifact_refs, ())
        self.assertTrue(result.receipt.suppressed_output_summary["execution_suppressed"])

    def test_receipt_write_and_ledger_append_are_local_artifacts(self) -> None:
        plan = build_validation_plan(
            minimal_manifest("python3 -c \"print('ok')\""),
            "repo:workspace-governance-control-fabric",
            tier="smoke",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            result = execute_validation_plan(
                plan,
                REPO_ROOT,
                temp_path / "artifacts",
                now="2026-04-30T00:00:00Z",
            )
            receipt_path = write_control_receipt(temp_path / "receipts" / "receipt.json", result.receipt)
            ledger_path = append_ledger_event(temp_path / "ledger.jsonl", result.ledger_event)
            append_ledger_event(ledger_path, result.ledger_event)

            self.assertEqual(
                json.loads(receipt_path.read_text(encoding="utf-8"))["receipt_id"],
                result.receipt.receipt_id,
            )
            self.assertEqual(len(ledger_path.read_text(encoding="utf-8").splitlines()), 2)
