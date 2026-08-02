"""Long-running Temporal worker runner for WGCF-owned activities."""

from __future__ import annotations

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from control_fabric_core.worker import (
    controlled_proof_worker_activation_status,
    controlled_proof_worker_settings,
    worker_settings,
)

from .activities import validation_readiness_activity


async def run_worker() -> None:
    """Connect to Temporal and poll only the WGCF activity task queue."""

    settings = worker_settings()
    client = await Client.connect(
        settings.address,
        namespace=settings.namespace,
        identity=settings.identity,
    )
    worker = Worker(
        client,
        task_queue=settings.task_queue,
        activities=[validation_readiness_activity],
    )
    await worker.run()


async def run_controlled_proof_worker() -> None:
    """Poll only the permit-bound queue and fail-stop on context revocation."""

    activation = controlled_proof_worker_activation_status()
    if not activation["authorized"]:
        raise RuntimeError("WGCF controlled-proof worker is not authorized")
    settings = controlled_proof_worker_settings()
    client = await Client.connect(
        settings.address,
        namespace=settings.namespace,
        identity=settings.identity,
    )
    worker = Worker(
        client,
        task_queue=settings.task_queue,
        activities=[validation_readiness_activity],
    )
    async with worker:
        await _wait_for_controlled_proof_context_revocation()


async def _wait_for_controlled_proof_context_revocation(
    *,
    poll_interval_seconds: float = 1.0,
) -> None:
    """Fail the worker task when its mounted authorization stops matching."""

    while True:
        await asyncio.sleep(poll_interval_seconds)
        activation = controlled_proof_worker_activation_status()
        if not activation["authorized"]:
            raise RuntimeError(
                "WGCF controlled-proof worker context was revoked",
            )
