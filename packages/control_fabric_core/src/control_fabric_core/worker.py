"""Worker settings and activation gates for WGCF-owned Temporal activities."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from os import environ
from pathlib import Path
from typing import Any

from .controlled_proof import (
    CONTROLLED_PROOF_ACTIVITY_TASK_QUEUE,
    CONTROLLED_PROOF_WORKER_ID,
    ControlledProofContractError,
    ControlledProofOwnerContext,
    load_controlled_proof_owner_context,
)
from .foundation import PACKAGE_VERSION, RUNTIME_REPO
from .orchestration_activities import (
    VALIDATION_READINESS_ACTIVITY_NAME,
    VALIDATION_READINESS_FAILURE_STATUS_CODES,
    VALIDATION_READINESS_RESULT_STATUS_CODES,
    VALIDATION_READINESS_TASK_QUEUE,
)


WORKER_STATUS = "activity-worker-source-ready"
WORKER_RUNTIME_MODE = "build-admitted-disabled"
CONTROLLED_PROOF_WORKER_STATUS = "controlled-proof-activity-worker-source-ready"
CONTROLLED_PROOF_WORKER_RUNTIME_MODE = "build-admitted-disabled"
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
CONTROLLED_PROOF_ENABLED_ENV = "WGCF_CONTROLLED_PROOF_ENABLED"
CONTROLLED_PROOF_EXECUTION_AUTHORIZED_ENV = (
    "WGCF_CONTROLLED_PROOF_EXECUTION_AUTHORIZED"
)
CONTROLLED_PROOF_CONTEXT_PATH_ENV = "WGCF_CONTROLLED_PROOF_CONTEXT_PATH"
CONTROLLED_PROOF_CONTEXT_DIGEST_ENV = "WGCF_CONTROLLED_PROOF_CONTEXT_DIGEST"
CONTROLLED_PROOF_EVIDENCE_ROOT_ENV = "WGCF_CONTROLLED_PROOF_EVIDENCE_ROOT"
CONTROLLED_PROOF_IMAGE_SOURCE_REVISION_PATH = Path(
    "/opt/wgcf/build/source-revision",
)
DEFAULT_CONTROLLED_PROOF_EVIDENCE_ROOT = Path(
    "/var/lib/wgcf/orchestration/controlled-proof",
)
CONTROLLED_PROOF_TEMPORAL_NAMESPACE_ENV = (
    "WGCF_CONTROLLED_PROOF_TEMPORAL_NAMESPACE"
)
CONTROLLED_PROOF_TEMPORAL_TASK_QUEUE_ENV = (
    "WGCF_CONTROLLED_PROOF_TEMPORAL_TASK_QUEUE"
)
CONTROLLED_PROOF_TEMPORAL_ADDRESS_ENV = "WGCF_CONTROLLED_PROOF_TEMPORAL_ADDRESS"
CONTROLLED_PROOF_TEMPORAL_WORKER_ID_ENV = (
    "WGCF_CONTROLLED_PROOF_TEMPORAL_WORKER_ID"
)
_SOURCE_REVISION_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


@dataclass(frozen=True)
class WorkerSettings:
    """Temporal-shaped settings that do not open a runtime connection."""

    namespace: str
    task_queue: str
    address: str
    identity: str

    def to_status(
        self,
        *,
        namespace_env_var: str = TEMPORAL_NAMESPACE_ENV,
        task_queue_env_var: str = TEMPORAL_TASK_QUEUE_ENV,
        address_env_var: str = TEMPORAL_ADDRESS_ENV,
    ) -> dict[str, Any]:
        return {
            "namespace_env_var": namespace_env_var,
            "task_queue_env_var": task_queue_env_var,
            "address_env_var": address_env_var,
            "namespace": self.namespace,
            "task_queue": self.task_queue,
            "address": self.address,
            "identity": self.identity,
            "ready_boundary": True,
            "connects_to_temporal": False,
            "sdk_dependency": "temporalio>=1.30,<2",
            "long_running_worker": False,
            "registered_activities": [VALIDATION_READINESS_ACTIVITY_NAME],
            "result_status_codes": list(VALIDATION_READINESS_RESULT_STATUS_CODES),
            "failure_status_codes": list(VALIDATION_READINESS_FAILURE_STATUS_CODES),
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


def controlled_proof_worker_settings() -> WorkerSettings:
    """Resolve the isolated controlled-proof worker settings."""

    return WorkerSettings(
        namespace=environ.get(
            CONTROLLED_PROOF_TEMPORAL_NAMESPACE_ENV,
            DEFAULT_TEMPORAL_NAMESPACE,
        ),
        task_queue=environ.get(
            CONTROLLED_PROOF_TEMPORAL_TASK_QUEUE_ENV,
            CONTROLLED_PROOF_ACTIVITY_TASK_QUEUE,
        ),
        address=environ.get(
            CONTROLLED_PROOF_TEMPORAL_ADDRESS_ENV,
            DEFAULT_TEMPORAL_ADDRESS,
        ),
        identity=environ.get(
            CONTROLLED_PROOF_TEMPORAL_WORKER_ID_ENV,
            CONTROLLED_PROOF_WORKER_ID,
        ),
    )


def controlled_proof_image_source_revision(
    source_revision_path: str | Path = CONTROLLED_PROOF_IMAGE_SOURCE_REVISION_PATH,
) -> str:
    """Read the immutable source revision baked into the worker image."""

    path = Path(source_revision_path)
    try:
        path_stat = path.lstat()
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ControlledProofContractError(
            "the worker image source provenance is unavailable",
        ) from exc
    if not stat.S_ISREG(path_stat.st_mode) or path.is_symlink():
        raise ControlledProofContractError(
            "the worker image source provenance must be a regular file",
        )
    if path_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ControlledProofContractError(
            "the worker image source provenance must not be group or world writable",
        )
    if path_stat.st_size not in {40, 41, 64, 65}:
        raise ControlledProofContractError(
            "the worker image source provenance is not a full source revision",
        )
    revision = raw.removesuffix("\n")
    if raw not in {revision, f"{revision}\n"} or not _SOURCE_REVISION_PATTERN.fullmatch(
        revision,
    ):
        raise ControlledProofContractError(
            "the worker image source provenance is not a full source revision",
        )
    return revision


def controlled_proof_evidence_root() -> Path:
    """Resolve the WGCF-owned evidence root for controlled proof execution."""

    return Path(
        environ.get(
            CONTROLLED_PROOF_EVIDENCE_ROOT_ENV,
            str(DEFAULT_CONTROLLED_PROOF_EVIDENCE_ROOT),
        ),
    ).resolve()


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


def controlled_proof_worker_activation_status(
    *,
    now: datetime | None = None,
    source_revision_path: str | Path = CONTROLLED_PROOF_IMAGE_SOURCE_REVISION_PATH,
) -> dict[str, Any]:
    """Evaluate the permit-derived gates for the isolated proof worker."""

    enabled = _env_true(CONTROLLED_PROOF_ENABLED_ENV)
    execution_authorized = _env_true(CONTROLLED_PROOF_EXECUTION_AUTHORIZED_ENV)
    context_path = environ.get(CONTROLLED_PROOF_CONTEXT_PATH_ENV, "").strip()
    context_digest = environ.get(CONTROLLED_PROOF_CONTEXT_DIGEST_ENV, "").strip()
    evidence_root = controlled_proof_evidence_root()
    source_revision: str | None = None
    settings = controlled_proof_worker_settings()
    blockers: list[str] = []
    context: ControlledProofOwnerContext | None = None

    if not enabled:
        blockers.append(f"{CONTROLLED_PROOF_ENABLED_ENV}=true is required")
    if not execution_authorized:
        blockers.append(
            f"{CONTROLLED_PROOF_EXECUTION_AUTHORIZED_ENV}=true is required",
        )
    if not context_path:
        blockers.append(f"{CONTROLLED_PROOF_CONTEXT_PATH_ENV} is required")
    if not context_digest:
        blockers.append(f"{CONTROLLED_PROOF_CONTEXT_DIGEST_ENV} is required")
    try:
        source_revision = controlled_proof_image_source_revision(
            source_revision_path,
        )
    except ControlledProofContractError:
        blockers.append("the worker image source provenance is invalid")
    if not evidence_root.is_dir() or not os.access(
        evidence_root,
        os.W_OK | os.X_OK,
    ):
        blockers.append("the controlled-proof evidence root is not writable")
    if settings.task_queue != CONTROLLED_PROOF_ACTIVITY_TASK_QUEUE:
        blockers.append(
            f"{CONTROLLED_PROOF_TEMPORAL_TASK_QUEUE_ENV} must be "
            f"{CONTROLLED_PROOF_ACTIVITY_TASK_QUEUE}",
        )
    if settings.identity != CONTROLLED_PROOF_WORKER_ID:
        blockers.append(
            f"{CONTROLLED_PROOF_TEMPORAL_WORKER_ID_ENV} must be "
            f"{CONTROLLED_PROOF_WORKER_ID}",
        )

    if context_path and context_digest:
        try:
            context = load_controlled_proof_owner_context(
                context_path,
                expected_digest=context_digest,
            )
        except ControlledProofContractError:
            blockers.append("the controlled-proof owner context is invalid")
    if context is not None:
        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if current_time < context.commissioning_session_started_at:
            blockers.append("the controlled-proof commissioning session has not started")
        if current_time >= context.authorization_expires_at:
            blockers.append("the controlled-proof authorization has expired")
        if settings.address != context.temporal_address:
            blockers.append("the Temporal address does not match the owner context")
        if settings.namespace != context.temporal_namespace:
            blockers.append("the Temporal namespace does not match the owner context")
        if settings.task_queue != context.activity_task_queue:
            blockers.append("the activity task queue does not match the owner context")
        if settings.identity != context.worker_identity:
            blockers.append("the worker identity does not match the owner context")
        if source_revision and source_revision != context.wgcf_source_revision:
            blockers.append(
                "the worker image source revision does not match the owner context",
            )

    return {
        "authorized": not blockers,
        "enabled": enabled,
        "execution_authorized": execution_authorized,
        "owner_context_id": context.owner_context_id if context else None,
        "owner_context_digest": context.owner_context_digest if context else None,
        "authorization_id": context.authorization_id if context else None,
        "image_source_revision": source_revision,
        "commissioning_session_id": (
            context.commissioning_session_id if context else None
        ),
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


def controlled_proof_worker_required_paths(repo_root: Path) -> dict[str, bool]:
    """Return the source contracts required by the controlled worker."""

    paths = {
        "apps/worker/README.md": repo_root / "apps/worker/README.md",
        "apps/worker/src/wgcf_worker/activities.py": (
            repo_root / "apps/worker/src/wgcf_worker/activities.py"
        ),
        "apps/worker/src/wgcf_worker/runner.py": (
            repo_root / "apps/worker/src/wgcf_worker/runner.py"
        ),
        "packages/control_fabric_core/src/control_fabric_core/controlled_proof.py": (
            repo_root
            / "packages/control_fabric_core/src/control_fabric_core/controlled_proof.py"
        ),
        "schemas/controlled-proof-owner-context.schema.json": (
            repo_root / "schemas/controlled-proof-owner-context.schema.json"
        ),
        "schemas/controlled-proof-activity-request.schema.json": (
            repo_root / "schemas/controlled-proof-activity-request.schema.json"
        ),
        "schemas/controlled-proof-owner-receipt.schema.json": (
            repo_root / "schemas/controlled-proof-owner-receipt.schema.json"
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


def controlled_proof_worker_status_snapshot(
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return the connection-free status of the controlled worker boundary."""

    root = Path(repo_root or ".").resolve()
    required_paths = controlled_proof_worker_required_paths(root)
    settings = controlled_proof_worker_settings()
    return {
        "repo": RUNTIME_REPO,
        "version": PACKAGE_VERSION,
        "status": CONTROLLED_PROOF_WORKER_STATUS,
        "runtime_mode": CONTROLLED_PROOF_WORKER_RUNTIME_MODE,
        "ready": all(required_paths.values()),
        "required_paths": required_paths,
        "temporal": settings.to_status(
            namespace_env_var=CONTROLLED_PROOF_TEMPORAL_NAMESPACE_ENV,
            task_queue_env_var=CONTROLLED_PROOF_TEMPORAL_TASK_QUEUE_ENV,
            address_env_var=CONTROLLED_PROOF_TEMPORAL_ADDRESS_ENV,
        ),
        "activation": controlled_proof_worker_activation_status(),
        "capabilities": [
            {
                "capability_id": "controlled-validation-readiness-proof",
                "purpose": (
                    "Execute only authorization-bound commissioning scenarios "
                    "and persist a WGCF owner receipt."
                ),
                "temporal_activity": VALIDATION_READINESS_ACTIVITY_NAME,
                "implemented": True,
            },
        ],
    }


def _env_true(name: str) -> bool:
    return environ.get(name, "").strip().lower() == "true"
