from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import (  # noqa: E402
    evaluate_admission_policy,
    project_receipt_to_art_completion_evidence,
    project_receipt_to_change_record_references,
    project_receipt_to_review_packet_evidence,
)


SAMPLE_RECEIPT = {
    "artifact_refs": [
        {
            "artifact_id": "artifact:aaaaaaaaaaaaaaaaaaaaaaaa",
            "byte_count": 37,
            "digest": "sha256:" + "b" * 64,
            "media_type": "text/plain; charset=utf-8",
            "path": ".wgcf/artifacts/check/stdout.log",
            "purpose": "validation-stdout",
        },
    ],
    "captured_at": "2026-04-30T00:00:00Z",
    "check_results": [
        {
            "artifact_refs": [
                {
                    "artifact_id": "artifact:aaaaaaaaaaaaaaaaaaaaaaaa",
                    "byte_count": 37,
                    "digest": "sha256:" + "b" * 64,
                    "media_type": "text/plain; charset=utf-8",
                    "path": ".wgcf/artifacts/check/stdout.log",
                    "purpose": "validation-stdout",
                },
            ],
            "check_id": "unit-tests",
            "command_digest": "sha256:" + "c" * 64,
            "duration_ms": 123,
            "error": None,
            "exit_code": 0,
            "output_summary": {"stdout": {"suppressed": True}},
            "required": True,
            "reused_receipt_id": None,
            "status": "success",
            "validator_id": "wgcf-unit-tests",
        },
    ],
    "digest": "sha256:" + "d" * 64,
    "manifest_id": "manifest:test",
    "outcome": "success",
    "plan_id": "validation-plan:test",
    "planner_decision": {"outcome": "planned"},
    "receipt_id": "control-receipt:dddddddddddddddddddddddd",
    "schema_version": 1,
    "suppressed_output_summary": {"raw_output_in_receipt": False},
    "target_scope": "repo:workspace-governance-control-fabric",
    "tier": "smoke",
}


def sample_policy_decision():
    return evaluate_admission_policy(
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
                    "digest": SAMPLE_RECEIPT["digest"],
                    "outcome": SAMPLE_RECEIPT["outcome"],
                    "receipt_id": SAMPLE_RECEIPT["receipt_id"],
                },
            ],
            "subject_id": "workspace-governance-control-fabric",
            "subject_type": "repo",
        },
        now="2026-04-30T00:00:00Z",
    )


class EvidenceProjectionTests(TestCase):
    def test_art_completion_projection_outputs_broker_payload_without_raw_artifacts(self) -> None:
        raw_marker = "RAW-CONTENT-SHOULD-NOT-APPEAR"
        projection = project_receipt_to_art_completion_evidence(
            SAMPLE_RECEIPT,
            changed_surfaces=[
                "`packages/control_fabric_core/src/control_fabric_core/evidence_projection.py`: adds projection adapters.",
            ],
            now="2026-04-30T01:00:00Z",
        )

        payload = projection.to_completion_payload()
        rendered_record = json.dumps(projection.to_record(), sort_keys=True)

        self.assertEqual(projection.projection.projection_type, "art_completion_evidence")
        self.assertFalse(projection.projection.raw_artifacts_embedded)
        self.assertIn("completion_summary", payload)
        self.assertIn("changed_surfaces", payload)
        self.assertIn(SAMPLE_RECEIPT["receipt_id"], payload["validation_evidence"])
        self.assertNotIn(raw_marker, rendered_record)

    def test_review_packet_projection_links_items_to_receipt(self) -> None:
        projection = project_receipt_to_review_packet_evidence(
            SAMPLE_RECEIPT,
            changed_surface_explanations=[
                "`schemas/evidence-projection.schema.json`: defines compact projection output.",
            ],
            item_ids=[451, 452, "453"],
            now="2026-04-30T01:00:00Z",
        )

        self.assertEqual(projection.projection.target_surface, "workspace-delivery-art-review-packet")
        self.assertEqual(len(projection.item_evidence_refs), 3)
        self.assertEqual(projection.item_evidence_refs[0]["receipt_id"], SAMPLE_RECEIPT["receipt_id"])
        self.assertIn("reverting the source change", projection.rollback_boundary)
        self.assertIn("wgcf-unit-tests", projection.test_evidence[0])

    def test_change_record_projection_references_receipt_artifacts_and_policy_decisions(self) -> None:
        policy_decision = sample_policy_decision()

        projection = project_receipt_to_change_record_references(
            SAMPLE_RECEIPT,
            change_record_path="docs/records/change-records/example.md",
            policy_decisions=[policy_decision],
            now="2026-04-30T01:00:00Z",
        )

        evidence_types = {entry["evidence_type"] for entry in projection.evidence_refs}
        self.assertEqual(projection.projection.projection_type, "change_record_references")
        self.assertIn("control_receipt", evidence_types)
        self.assertIn("artifact_ref", evidence_types)
        self.assertIn("policy_decision", evidence_types)
        self.assertIn(SAMPLE_RECEIPT["receipt_id"], projection.record_note)
        self.assertIn("do not copy raw runtime artifacts", projection.record_note)

    def test_projection_rejects_receipt_without_required_refs(self) -> None:
        invalid_receipt = dict(SAMPLE_RECEIPT)
        invalid_receipt.pop("receipt_id")

        with self.assertRaises(ValueError):
            project_receipt_to_art_completion_evidence(
                invalid_receipt,
                changed_surfaces=["`surface`: change."],
                now="2026-04-30T01:00:00Z",
            )

    def test_projection_schema_file_exists(self) -> None:
        self.assertTrue((REPO_ROOT / "schemas/evidence-projection.schema.json").is_file())
