from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import (  # noqa: E402
    build_art_runtime_graph,
    evaluate_art_readiness,
    project_receipts_to_art_evidence_packet,
)


def continuation_context(*, projection_dirty: bool = False, with_headings: bool = True) -> dict:
    target_item = {
        "id": 517,
        "type": "Feature",
        "subject": "Enabler: Optimize ART runtime context, quality, and readiness through WGCF",
        "status": "ready",
        "owner_repo": "workspace-governance-control-fabric",
        "delivery_team": "Platform Architecture",
        "iteration": "PI-2026-03 / Iteration 1",
        "target_pi": "PI-2026-03",
        "parent_id": 498,
        "record_ref": "openproject://work_packages/517",
        "descriptionPresent": with_headings,
        "descriptionHeadings": [
            "What This Enables",
            "Benefit Hypothesis",
            "Scope Boundaries",
            "Evidence Expectation",
            "Execution Context",
            "Operator work notes",
        ]
        if with_headings
        else [],
    }
    return {
        "continuation_context": {
            "delivery_epic": {
                "id": 498,
                "type": "Epic",
                "subject": "Optimize governance validator invocation and ART integration through WGCF",
                "status": "in-progress",
                "owner_repo": "workspace-governance-control-fabric",
                "target_pi": "PI-2026-03",
                "record_ref": "openproject://work_packages/498",
            },
            "summary": {
                "completed_related_count": 3,
                "open_child_count": 4,
            },
            "target_item": target_item,
            "initiative_next_ready_items": [
                target_item,
                {
                    "id": 518,
                    "type": "User story",
                    "subject": "Enabler: Ingest OOS continuation state into the WGCF ART graph",
                    "status": "ready",
                    "owner_repo": "workspace-governance-control-fabric",
                    "delivery_team": "Platform Architecture",
                    "iteration": "PI-2026-03 / Iteration 1",
                    "target_pi": "PI-2026-03",
                    "parent_id": 517,
                    "record_ref": "openproject://work_packages/518",
                },
            ],
        },
        "projection_state": {
            "affected_delivery_ids": ["delivery-498"] if projection_dirty else [],
            "affected_work_item_ids": [517] if projection_dirty else [],
            "dirty": projection_dirty,
            "next_action": "sync projection" if projection_dirty else "No projection checkpoint is pending.",
        },
    }


SAMPLE_RECEIPT = {
    "artifact_refs": [
        {
            "artifact_id": "artifact:aaaaaaaaaaaaaaaaaaaaaaaa",
            "byte_count": 27,
            "digest": "sha256:" + "a" * 64,
            "media_type": "text/plain; charset=utf-8",
            "path": ".wgcf/artifacts/stdout.log",
            "purpose": "validation-stdout",
        },
    ],
    "captured_at": "2026-05-01T00:00:00Z",
    "check_results": [
        {
            "check_id": "wgcf-tests",
            "exit_code": 0,
            "status": "success",
            "validator_id": "wgcf-unit-tests",
        },
    ],
    "digest": "sha256:" + "b" * 64,
    "outcome": "success",
    "receipt_id": "control-receipt:bbbbbbbbbbbbbbbbbbbbbbbb",
    "target_scope": "art:delivery-498",
}


class ArtReadinessTests(TestCase):
    def test_art_runtime_graph_ingests_broker_context_without_raw_context(self) -> None:
        graph = build_art_runtime_graph(
            continuation_context(),
            now="2026-05-01T00:00:00Z",
        )

        node_ids = {node.node_id for node in graph.nodes}
        edge_types = {edge.edge_type for edge in graph.edges}
        record = graph.to_record()

        self.assertIn("art-item:517", node_ids)
        self.assertIn("art-item:518", node_ids)
        self.assertIn("parent-of", edge_types)
        self.assertEqual(record["summary"]["projection_dirty"], False)
        self.assertIn("continuation_context", record["source_surfaces"])

    def test_readiness_blocks_dirty_projection_and_weak_feature_narrative(self) -> None:
        readiness = evaluate_art_readiness(
            continuation_context(projection_dirty=True, with_headings=False),
            operation="complete",
            target_item_id=517,
            now="2026-05-01T00:00:00Z",
        )

        finding_codes = {finding.code for finding in readiness.findings}
        actions = {recommendation.action for recommendation in readiness.recommendations}

        self.assertFalse(readiness.mutation_allowed)
        self.assertEqual(readiness.outcome, "blocked")
        self.assertIn("projection-sync-required", finding_codes)
        self.assertIn("weak-feature-narrative", finding_codes)
        self.assertIn("projection_sync", actions)
        self.assertIn("repair_art_metadata", actions)
        self.assertTrue(readiness.projection_sync_recommended)

    def test_readiness_allows_clean_context_with_required_feature_headings(self) -> None:
        readiness = evaluate_art_readiness(
            continuation_context(),
            operation="complete",
            target_item_id=517,
            now="2026-05-01T00:00:00Z",
        )

        self.assertTrue(readiness.mutation_allowed)
        self.assertEqual(readiness.outcome, "ready")
        self.assertEqual(readiness.findings, ())
        self.assertEqual(readiness.recommendations[0].action, "proceed_via_oos_broker")
        self.assertFalse(readiness.raw_context_embedded)

    def test_readiness_blocks_unhealthy_quality_pack(self) -> None:
        context = continuation_context()
        context["quality_pack"] = {
            "checks": [
                {
                    "name": "art-quality",
                    "status": "failed",
                },
            ],
        }

        readiness = evaluate_art_readiness(
            context,
            operation="complete",
            target_item_id=517,
            now="2026-05-01T00:00:00Z",
        )

        self.assertFalse(readiness.mutation_allowed)
        self.assertIn("quality_pack", readiness.source_surfaces)
        self.assertIn("quality-pack-unhealthy", {finding.code for finding in readiness.findings})
        self.assertIn("record_blocker", {recommendation.action for recommendation in readiness.recommendations})

    def test_stale_open_parent_gets_bounded_closeout_recommendation(self) -> None:
        context = continuation_context()
        context["continuation_context"]["summary"]["open_child_count"] = 0

        readiness = evaluate_art_readiness(
            context,
            operation="stale-open-close",
            target_item_id=517,
            now="2026-05-01T00:00:00Z",
        )

        self.assertTrue(readiness.mutation_allowed)
        self.assertEqual(readiness.outcome, "review_required")
        self.assertEqual(readiness.findings[0].code, "stale-open-parent")
        self.assertEqual(readiness.recommendations[0].action, "stale_open_close")

    def test_evidence_packet_is_completion_preflight_compatible(self) -> None:
        raw_marker = "RAW-OUTPUT-MUST-NOT-APPEAR"
        receipt = dict(SAMPLE_RECEIPT)
        receipt["raw_marker"] = raw_marker

        packet = project_receipts_to_art_evidence_packet(
            [receipt],
            changed_surfaces=["`packages/control_fabric_core/src/control_fabric_core/art_readiness.py`: adds ART readiness."],
            completion_summary="Completed ART readiness projection.",
            item_ids=[517, 518],
            now="2026-05-01T00:00:00Z",
        )
        record = packet.to_record()
        rendered_record = json.dumps(record, sort_keys=True)

        self.assertFalse(packet.raw_artifacts_embedded)
        self.assertNotIn(raw_marker, rendered_record)
        self.assertIn("- PASS:", record["completion_payload"]["test_result_evidence"])
        self.assertIn("- PASS:", record["completion_payload"]["validation_evidence"])
        self.assertEqual(len(packet.item_evidence_refs), 2)

    def test_art_schema_files_exist(self) -> None:
        self.assertTrue((REPO_ROOT / "schemas/art-readiness-receipt.schema.json").is_file())
        self.assertTrue((REPO_ROOT / "schemas/art-evidence-packet.schema.json").is_file())
