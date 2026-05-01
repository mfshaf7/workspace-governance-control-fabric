from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import (  # noqa: E402
    InvocationClass,
    coerce_execution_limits,
    evaluate_operation_budget,
    evaluate_validation_plan_budget,
    operation_budget_records,
    paginate_items,
    resolve_performance_budget,
)


class PerformanceBudgetTests(TestCase):
    def test_art_and_draft_operations_have_distinct_invocation_classes(self) -> None:
        continuation = resolve_performance_budget("art.continuation")
        submit = resolve_performance_budget("draft.submit")
        complete = resolve_performance_budget("art.complete")
        projection = resolve_performance_budget("projection.sync")

        self.assertEqual(continuation.invocation_class, InvocationClass.INLINE_FAST.value)
        self.assertEqual(submit.invocation_class, InvocationClass.RECEIPT_CHECK.value)
        self.assertEqual(submit.recommended_action, "verify_payload_digest_and_fresh_receipt")
        self.assertEqual(complete.invocation_class, InvocationClass.HARD_GATE.value)
        self.assertEqual(projection.invocation_class, InvocationClass.CHECKPOINT_BATCH.value)

    def test_unknown_operation_requires_classification_before_sync_gate(self) -> None:
        decision = evaluate_operation_budget("new.future.component").to_record()

        self.assertFalse(decision["within_budget"])
        self.assertEqual(decision["invocation_class"], "offline-advisory")
        self.assertEqual(decision["recommended_action"], "classify_operation_before_synchronous_gate")

    def test_validation_plan_budget_routes_large_inline_plan_to_checkpoint(self) -> None:
        decision = evaluate_validation_plan_budget(
            selected_check_count=25,
            tier="scoped",
        ).to_record()

        self.assertFalse(decision["within_budget"])
        self.assertEqual(decision["recommended_action"], "route_to_checkpoint_batch_or_paginate")
        self.assertIn("selected_checks 25 exceeds budget 20", decision["reasons"])

    def test_execution_limits_cap_unbounded_manifest_policy(self) -> None:
        limits = coerce_execution_limits(
            {
                "output_budget_bytes": 999_999_999,
                "retry_count": 20,
                "timeout_seconds": 999,
            },
            default_timeout_seconds=120,
        )
        record = limits.to_record()

        self.assertEqual(limits.timeout_seconds, 120)
        self.assertEqual(limits.retry_count, 3)
        self.assertEqual(limits.output_budget_bytes, 500_000)
        self.assertFalse(record["within_budget"])
        self.assertIn("retry_count capped from 20 to 3", record["reasons"])

    def test_enterprise_profile_tightens_validation_run_limits(self) -> None:
        limits = coerce_execution_limits(
            {
                "output_budget_bytes": 500_000,
                "retry_count": 3,
                "timeout_seconds": 120,
            },
            default_timeout_seconds=120,
            profile="enterprise",
        )

        self.assertEqual(limits.retry_count, 1)
        self.assertEqual(limits.output_budget_bytes, 250_000)

    def test_pagination_applies_graph_budget_page_size(self) -> None:
        page, pagination = paginate_items(tuple(range(100)), limit=100, operation="graph.query")

        self.assertEqual(len(page), 50)
        self.assertTrue(pagination.has_next_page)
        self.assertEqual(pagination.next_offset, 50)

    def test_budget_records_are_operator_inspectable(self) -> None:
        records = operation_budget_records(["art.continuation", "draft.submit"])

        self.assertEqual([record["operation"] for record in records], ["art.continuation", "draft.submit"])
        self.assertEqual(records[0]["invocation_class"], "inline-fast")
