"""Worker scaffold helpers for the control-fabric runtime.

The worker surface is Temporal-ready without importing or connecting to a
Temporal runtime yet. This keeps Phase 1 source work testable while preserving
the task-queue and workflow vocabulary needed for the later runtime lane.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from os import environ
from pathlib import Path
from typing import Any

from .foundation import PACKAGE_VERSION, RUNTIME_REPO


WORKER_STATUS = "worker-skeleton"
WORKER_RUNTIME_MODE = "local-scaffold"
WORKER_ENTRYPOINT_PATH = Path("apps/worker/src/wgcf_worker/main.py")
TEMPORAL_NAMESPACE_ENV = "WGCF_TEMPORAL_NAMESPACE"
TEMPORAL_TASK_QUEUE_ENV = "WGCF_TEMPORAL_TASK_QUEUE"
TEMPORAL_ADDRESS_ENV = "WGCF_TEMPORAL_ADDRESS"
DEFAULT_TEMPORAL_NAMESPACE = "default"
DEFAULT_TEMPORAL_TASK_QUEUE = "workspace-governance-control-fabric"
DEFAULT_TEMPORAL_ADDRESS = "127.0.0.1:7233"


@dataclass(frozen=True)
class WorkerSettings:
    """Temporal-shaped settings that do not open a runtime connection."""

    namespace: str
    task_queue: str
    address: str
    identity: str

    def to_status(self) -> dict[str, Any]:
        return {
            "namespace_env_var": TEMPORAL_NAMESPACE_ENV,
            "task_queue_env_var": TEMPORAL_TASK_QUEUE_ENV,
            "address_env_var": TEMPORAL_ADDRESS_ENV,
            "namespace": self.namespace,
            "task_queue": self.task_queue,
            "address": self.address,
            "identity": self.identity,
            "ready_boundary": True,
            "connects_to_temporal": False,
            "sdk_dependency": "deferred",
            "long_running_worker": False,
        }


@dataclass(frozen=True)
class WorkerCapability:
    """Declared future worker capability without executable workflow behavior."""

    capability_id: str
    purpose: str
    temporal_workflow_hint: str
    implemented: bool


PLANNED_WORKER_CAPABILITIES = (
    WorkerCapability(
        capability_id="source-snapshot-ingest",
        purpose="Ingest authority-source snapshots for later validation planning.",
        temporal_workflow_hint="SourceSnapshotIngestWorkflow",
        implemented=False,
    ),
    WorkerCapability(
        capability_id="validation-plan-execute",
        purpose="Execute scoped validation plans and summarize bounded evidence.",
        temporal_workflow_hint="ValidationPlanExecutionWorkflow",
        implemented=False,
    ),
    WorkerCapability(
        capability_id="control-receipt-append",
        purpose="Append receipt and ledger records after validation or readiness actions.",
        temporal_workflow_hint="ControlReceiptAppendWorkflow",
        implemented=False,
    ),
)


def worker_settings() -> WorkerSettings:
    """Resolve worker settings without connecting to Temporal or other services."""

    namespace = environ.get(TEMPORAL_NAMESPACE_ENV, DEFAULT_TEMPORAL_NAMESPACE)
    task_queue = environ.get(TEMPORAL_TASK_QUEUE_ENV, DEFAULT_TEMPORAL_TASK_QUEUE)
    address = environ.get(TEMPORAL_ADDRESS_ENV, DEFAULT_TEMPORAL_ADDRESS)
    return WorkerSettings(
        namespace=namespace,
        task_queue=task_queue,
        address=address,
        identity=f"{RUNTIME_REPO}-local-worker",
    )


def worker_required_paths(repo_root: Path) -> dict[str, bool]:
    """Return the worker scaffold file checks this repo must satisfy."""

    paths = {
        "apps/worker/README.md": repo_root / "apps/worker/README.md",
        "apps/worker/src/wgcf_worker/__init__.py": (
            repo_root / "apps/worker/src/wgcf_worker/__init__.py"
        ),
        "apps/worker/src/wgcf_worker/__main__.py": (
            repo_root / "apps/worker/src/wgcf_worker/__main__.py"
        ),
        str(WORKER_ENTRYPOINT_PATH): repo_root / WORKER_ENTRYPOINT_PATH,
    }
    return {name: path.exists() and path.is_file() for name, path in paths.items()}


def worker_status_snapshot(repo_root: str | Path | None = None) -> dict[str, Any]:
    """Return a compact, operator-safe worker scaffold status."""

    root = Path(repo_root or ".").resolve()
    required_paths = worker_required_paths(root)
    return {
        "repo": RUNTIME_REPO,
        "version": PACKAGE_VERSION,
        "status": WORKER_STATUS,
        "runtime_mode": WORKER_RUNTIME_MODE,
        "ready": all(required_paths.values()),
        "required_paths": required_paths,
        "temporal": worker_settings().to_status(),
        "capabilities": [asdict(capability) for capability in PLANNED_WORKER_CAPABILITIES],
    }
