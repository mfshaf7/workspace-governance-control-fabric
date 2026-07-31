from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from temporalio.exceptions import ApplicationError


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))
sys.path.insert(0, str(REPO_ROOT / "apps/worker/src"))

from control_fabric_core.orchestration_activities import (
    ValidationReadinessContractError,
)
from wgcf_worker.activities import validation_readiness_activity


def valid_request() -> dict[str, object]:
    return {
        "schema_version": 1,
        "definition_id": "validation-readiness-run",
        "definition_version": 1,
        "run_id": "run:698-proof-01",
        "workflow_id": "workflow:validation-readiness-698",
        "source_ref": "art:delivery-698",
        "source_version": "commit:abc1234",
        "validation_scope": "component:workspace-governance",
        "readiness_target": "repo:workspace-governance-control-fabric",
        "profile": "local-read-only",
        "tier": "smoke",
        "correlation_id": "correlation:delivery-698",
        "causation_id": "causation:delivery-719",
        "idempotency_key": "idempotency:delivery-698-proof-01",
        "caller_id": "operator-orchestration-service",
        "operator_id": "operator:test",
    }


def activity_info() -> SimpleNamespace:
    return SimpleNamespace(
        activity_id="activity:validation-readiness",
        attempt=1,
        workflow_id="workflow:validation-readiness-698",
    )


class WorkerActivityTests(IsolatedAsyncioTestCase):
    async def test_contract_rejection_is_non_retryable_and_suppresses_detail(
        self,
    ) -> None:
        with (
            patch("wgcf_worker.activities.activity.info", return_value=activity_info()),
            patch(
                "wgcf_worker.activities.asyncio.to_thread",
                new=AsyncMock(
                    side_effect=ValidationReadinessContractError(
                        "raw contract detail",
                    ),
                ),
            ),
        ):
            with self.assertRaises(ApplicationError) as raised:
                await validation_readiness_activity(valid_request())

        self.assertEqual(raised.exception.type, "WGCF_CONTRACT_REJECTED")
        self.assertTrue(raised.exception.non_retryable)
        self.assertNotIn("raw contract detail", str(raised.exception))

    async def test_unavailable_failure_is_retryable_and_suppresses_detail(
        self,
    ) -> None:
        with (
            patch("wgcf_worker.activities.activity.info", return_value=activity_info()),
            patch(
                "wgcf_worker.activities.asyncio.to_thread",
                new=AsyncMock(side_effect=OSError("raw filesystem detail")),
            ),
        ):
            with self.assertRaises(ApplicationError) as raised:
                await validation_readiness_activity(valid_request())

        self.assertEqual(raised.exception.type, "WGCF_ACTIVITY_UNAVAILABLE")
        self.assertFalse(raised.exception.non_retryable)
        self.assertNotIn("raw filesystem detail", str(raised.exception))

    async def test_unexpected_failure_is_retryable_and_suppresses_detail(
        self,
    ) -> None:
        with (
            patch("wgcf_worker.activities.activity.info", return_value=activity_info()),
            patch(
                "wgcf_worker.activities.asyncio.to_thread",
                new=AsyncMock(side_effect=RuntimeError("raw internal detail")),
            ),
        ):
            with self.assertRaises(ApplicationError) as raised:
                await validation_readiness_activity(valid_request())

        self.assertEqual(raised.exception.type, "WGCF_ACTIVITY_RETRYABLE")
        self.assertFalse(raised.exception.non_retryable)
        self.assertNotIn("raw internal detail", str(raised.exception))

    async def test_temporal_cancellation_is_not_repackaged(self) -> None:
        with (
            patch("wgcf_worker.activities.activity.info", return_value=activity_info()),
            patch(
                "wgcf_worker.activities.asyncio.to_thread",
                new=AsyncMock(side_effect=asyncio.CancelledError()),
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await validation_readiness_activity(valid_request())
