from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import (
    apply_retention_plan,
    build_retention_plan,
    retention_thresholds,
)


class LifecycleTests(TestCase):
    def test_retention_plan_is_dry_run_and_identifies_old_local_state(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            root = Path(temp_dir)
            old_artifact = root / ".wgcf/artifacts/old/stdout.txt"
            new_artifact = root / ".wgcf/artifacts/new/stdout.txt"
            old_receipt = root / ".wgcf/receipts/old.json"
            new_receipt = root / ".wgcf/receipts/new.json"
            for path in (old_artifact, new_artifact, old_receipt, new_receipt):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.name, encoding="utf-8")
            _set_mtime(old_artifact, "2026-01-01T00:00:00Z")
            _set_mtime(old_receipt, "2026-01-01T00:00:00Z")

            plan = build_retention_plan(
                artifact_root=".wgcf/artifacts",
                ledger_path=".wgcf/ledger.jsonl",
                now="2026-05-01T00:00:00Z",
                profile="developer",
                receipt_dir=".wgcf/receipts",
                repo_root=root,
            )

            self.assertEqual(plan.profile, "developer")
            self.assertTrue(plan.requires_confirmation)
            self.assertEqual(plan.summary["artifact_delete_count"], 1)
            self.assertEqual(plan.summary["receipt_delete_count"], 1)
            self.assertTrue(old_artifact.is_file())
            self.assertTrue(old_receipt.is_file())
            self.assertIn("older-than-14-days", plan.artifact_candidates[0].reason)
            self.assertIn("older-than-30-days", plan.receipt_candidates[0].reason)

    def test_apply_without_confirm_is_blocked_and_mutates_nothing(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            root = Path(temp_dir)
            old_artifact = root / ".wgcf/artifacts/old/stdout.txt"
            old_artifact.parent.mkdir(parents=True, exist_ok=True)
            old_artifact.write_text("old", encoding="utf-8")
            _set_mtime(old_artifact, "2026-01-01T00:00:00Z")

            result = apply_retention_plan(
                artifact_root=".wgcf/artifacts",
                confirm=False,
                ledger_path=".wgcf/ledger.jsonl",
                now="2026-05-01T00:00:00Z",
                receipt_dir=".wgcf/receipts",
                repo_root=root,
            )

            self.assertEqual(result.outcome, "blocked")
            self.assertIsNone(result.ledger_event)
            self.assertTrue(old_artifact.is_file())
            self.assertFalse((root / ".wgcf/ledger.jsonl").exists())

    def test_confirmed_apply_deletes_candidates_and_records_lifecycle_event(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            root = Path(temp_dir)
            old_artifact = root / ".wgcf/artifacts/old/stdout.txt"
            new_artifact = root / ".wgcf/artifacts/new/stdout.txt"
            old_receipt = root / ".wgcf/receipts/old.json"
            for path in (old_artifact, new_artifact, old_receipt):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.name, encoding="utf-8")
            _set_mtime(old_artifact, "2026-01-01T00:00:00Z")
            _set_mtime(old_receipt, "2026-01-01T00:00:00Z")

            result = apply_retention_plan(
                actor="test-operator",
                artifact_root=".wgcf/artifacts",
                confirm=True,
                ledger_path=".wgcf/ledger.jsonl",
                now="2026-05-01T00:00:00Z",
                receipt_dir=".wgcf/receipts",
                repo_root=root,
            )

            self.assertEqual(result.outcome, "success")
            self.assertFalse(old_artifact.exists())
            self.assertFalse(old_receipt.exists())
            self.assertTrue(new_artifact.is_file())
            self.assertEqual(result.ledger_event["action"], "lifecycle.retention.applied")
            self.assertEqual(result.ledger_event["record_refs"][0]["record_id"], result.plan.plan_id)
            self.assertTrue(Path(result.ledger_event_path or "").is_file())

    def test_ledger_compaction_exports_old_lines_before_rewriting_ledger(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            root = Path(temp_dir)
            ledger = root / ".wgcf/ledger.jsonl"
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text(
                "".join(json.dumps({"event": index}) + "\n" for index in range(1002)),
                encoding="utf-8",
            )

            result = apply_retention_plan(
                artifact_root=".wgcf/artifacts",
                confirm=True,
                export_dir=".wgcf/ledger-exports",
                ledger_path=".wgcf/ledger.jsonl",
                now="2026-05-01T00:00:00Z",
                profile="ci",
                receipt_dir=".wgcf/receipts",
                repo_root=root,
            )

            self.assertEqual(result.outcome, "success")
            self.assertIsNotNone(result.ledger_export_ref)
            export_path = Path(result.ledger_export_ref.path)
            self.assertTrue(export_path.is_file())
            self.assertEqual(len(export_path.read_text(encoding="utf-8").splitlines()), 3)
            self.assertLessEqual(len(ledger.read_text(encoding="utf-8").splitlines()), 1000)
            self.assertIn("lifecycle.retention.applied", ledger.read_text(encoding="utf-8"))

    def test_retention_profile_rejects_unknown_profile(self) -> None:
        with self.assertRaises(ValueError):
            retention_thresholds("unknown")

    def test_retention_paths_must_stay_inside_repo_root(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
            root = Path(temp_dir)
            with self.assertRaises(ValueError):
                build_retention_plan(
                    artifact_root="/tmp",
                    ledger_path=".wgcf/ledger.jsonl",
                    receipt_dir=".wgcf/receipts",
                    repo_root=root,
                )


def _set_mtime(path: Path, timestamp: str) -> None:
    text = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    epoch = datetime.fromisoformat(text).astimezone(UTC).timestamp()
    os.utime(path, (epoch, epoch))
