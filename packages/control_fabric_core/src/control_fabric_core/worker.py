"""Worker settings and activation gates for WGCF-owned Temporal activities."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from os import environ
from pathlib import Path
from typing import Any

from .foundation import PACKAGE_VERSION, RUNTIME_REPO
from .orchestration_activities import (
    VALIDATION_READINESS_ACTIVITY_NAME,
    VALIDATION_READINESS_TASK_QUEUE,
)


WORKER_STATUS = "activity-worker-source-ready"
WORKER_RUNTIME_MODE = "build-admitted-disabled"
WORKER_ENTRYPOINT_PATH = Path("apps/worker/src/wgcf_worker/main.py")
TEMPORAL_NAMESPACE_ENV = "WGCF_TEMPORAL_NAMESPACE"
TEMPORAL_TASK_QUEUE_ENV = "WGCF_TEMPORAL_TASK_QUEUE"
TEMPORAL_ADDRESS_ENV = "WGCF_TEMPORAL_ADDRESS"
TEMPORAL_WORKER_ID_ENV = "WGCF_TEMPORAL_WORKER_ID"
TEMPORAL_WORKER_ENABLED_ENV = "WGCF_TEMPORAL_WORKER_ENABLED"
TEMPORAL_ACTIVITY_EXECUTION_AUTHORIZED_ENV = (
    "WGCF_TEMPORAL_ACTIVITY_EXECUTION_AUTHORIZED"
)
TEMPORAL_ACTIVATION_REVIEW_REF_ENV = "WGCF_TEMPORAL_ACTIVATION_REVIEW_REF"
DEFAULT_TEMPORAL_NAMESPACE = "default"
DEFAULT_TEMPORAL_TASK_QUEUE = VALIDATION_READINESS_TASK_QUEUE
DEFAULT_TEMPORAL_ADDRESS = "127.0.0.1:7233"
DEFAULT_TEMPORAL_WORKER_ID = "wgcf-activity-worker"


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
            "sdk_dependency": "temporalio>=1.30,<2",
            "long_running_worker": False,
            "registered_activities": [VALIDATION_READINESS_ACTIVITY_NAME],
        }


@dataclass(frozen=True)
class WorkerCapability:
    """Declared owner capability exposed by this worker source."""

    capability_id: str
    purpose: str
    temporal_activity: str | None
    implemented: bool


WORKER_CAPABILITIES = (
    WorkerCapability(
        capability_id="validation-readiness",
        purpose=(
            "Execute the bounded validation/readiness proof and return compact "
            "receipt evidence to the OOS-owned workflow."
        ),
        temporal_activity=VALIDATION_READINESS_ACTIVITY_NAME,
        implemented=True,
    ),
    WorkerCapability(
        capability_id="aggregate-workflow-control",
        purpose="Aggregate workflow and retry control remain owned by OOS.",
        temporal_activity=None,
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
        identity=environ.get(TEMPORAL_WORKER_ID_ENV, DEFAULT_TEMPORAL_WORKER_ID),
    )


def worker_activation_status() -> dict[str, Any]:
    """Return the explicit gates required before a worker may connect."""

    enabled = _env_true(TEMPORAL_WORKER_ENABLED_ENV)
    execution_authorized = _env_true(TEMPORAL_ACTIVITY_EXECUTION_AUTHORIZED_ENV)
    review_ref = environ.get(TEMPORAL_ACTIVATION_REVIEW_REF_ENV, "").strip()
    settings = worker_settings()
    blockers: list[str] = []
    if not enabled:
        blockers.append(f"{TEMPORAL_WORKER_ENABLED_ENV}=true is required")
    if not execution_authorized:
        blockers.append(
            f"{TEMPORAL_ACTIVITY_EXECUTION_AUTHORIZED_ENV}=true is required",
        )
    if not review_ref:
        blockers.append(f"{TEMPORAL_ACTIVATION_REVIEW_REF_ENV} is required")
    if settings.task_queue != VALIDATION_READINESS_TASK_QUEUE:
        blockers.append(
            f"{TEMPORAL_TASK_QUEUE_ENV} must be {VALIDATION_READINESS_TASK_QUEUE}",
        )
    if settings.identity != DEFAULT_TEMPORAL_WORKER_ID:
        blockers.append(
            f"{TEMPORAL_WORKER_ID_ENV} must be {DEFAULT_TEMPORAL_WORKER_ID}",
        )
    return {
        "authorized": not blockers,
        "enabled": enabled,
        "activity_execution_authorized": execution_authorized,
        "activation_review_ref": review_ref or None,
        "blockers": blockers,
    }


def worker_required_paths(repo_root: Path) -> dict[str, bool]:
    """Return the worker source files this repo must provide."""

    paths = {
        "apps/worker/README.md": repo_root / "apps/worker/README.md",
        "apps/worker/src/wgcf_worker/__init__.py": (
            repo_root / "apps/worker/src/wgcf_worker/__init__.py"
        ),
        "apps/worker/src/wgcf_worker/__main__.py": (
            repo_root / "apps/worker/src/wgcf_worker/__main__.py"
        ),
        "apps/worker/src/wgcf_worker/activities.py": (
            repo_root / "apps/worker/src/wgcf_worker/activities.py"
        ),
        "apps/worker/src/wgcf_worker/runner.py": (
            repo_root / "apps/worker/src/wgcf_worker/runner.py"
        ),
        str(WORKER_ENTRYPOINT_PATH): repo_root / WORKER_ENTRYPOINT_PATH,
        "schemas/validation-readiness-activity-request.schema.json": (
            repo_root / "schemas/validation-readiness-activity-request.schema.json"
        ),
        "schemas/validation-readiness-activity-result.schema.json": (
            repo_root / "schemas/validation-readiness-activity-result.schema.json"
        ),
    }
    return {name: path.exists() and path.is_file() for name, path in paths.items()}


def worker_status_snapshot(repo_root: str | Path | None = None) -> dict[str, Any]:
    """Return compact, connection-free worker source and activation status."""

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
        "activation": worker_activation_status(),
        "capabilities": [asdict(capability) for capability in WORKER_CAPABILITIES],
    }


def _env_true(name: str) -> bool:
    return environ.get(name, "").strip().lower() == "true"
