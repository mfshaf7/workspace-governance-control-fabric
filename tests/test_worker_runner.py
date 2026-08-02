from __future__ import annotations

import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))
sys.path.insert(0, str(REPO_ROOT / "apps/worker/src"))

from wgcf_worker.runner import (
    _wait_for_controlled_proof_context_revocation,
    run_controlled_proof_worker,
)


class ControlledProofWorkerRunnerTests(IsolatedAsyncioTestCase):
    async def test_direct_runner_refuses_before_temporal_connection(self) -> None:
        with (
            patch(
                "wgcf_worker.runner.controlled_proof_worker_activation_status",
                return_value={"authorized": False},
            ),
            patch(
                "wgcf_worker.runner.Client.connect",
                new=AsyncMock(),
            ) as connect,
        ):
            with self.assertRaisesRegex(RuntimeError, "not authorized"):
                await run_controlled_proof_worker()

        connect.assert_not_awaited()

    async def test_context_monitor_fail_stops_after_revocation(self) -> None:
        with (
            patch(
                "wgcf_worker.runner.controlled_proof_worker_activation_status",
                side_effect=[{"authorized": True}, {"authorized": False}],
            ),
            patch(
                "wgcf_worker.runner.asyncio.sleep",
                new=AsyncMock(),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "context was revoked"):
                await _wait_for_controlled_proof_context_revocation(
                    poll_interval_seconds=0,
                )
