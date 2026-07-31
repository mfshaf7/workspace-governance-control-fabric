"""Long-running Temporal worker runner for WGCF-owned activities."""

from __future__ import annotations

from temporalio.client import Client
from temporalio.worker import Worker

from control_fabric_core.worker import worker_settings

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
