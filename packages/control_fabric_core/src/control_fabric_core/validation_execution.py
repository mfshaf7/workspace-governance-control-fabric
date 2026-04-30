"""Bounded validation execution with compact receipts and ledger events.

This module executes commands that were already selected by the validation
planner from a governance manifest. It does not decide policy, mutate upstream
authority stores, or print raw validator output into receipts.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .validation_planning import (
    ValidationExecutionMode,
    ValidationPlan,
)


RECEIPT_SCHEMA_VERSION = 1
LEDGER_SCHEMA_VERSION = 1
COMMAND_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class ValidationArtifactRef:
    """Reference to a local artifact containing full command output."""

    artifact_id: str
    byte_count: int
    digest: str
    media_type: str
    path: str
    purpose: str

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationCheckResult:
    """Compact result for one planned validation check."""

    artifact_refs: tuple[ValidationArtifactRef, ...]
    check_id: str
    command_digest: str | None
    duration_ms: int | None
    error: str | None
    exit_code: int | None
    output_summary: dict[str, Any]
    required: bool
    reused_receipt_id: str | None
    status: str
    validator_id: str

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["artifact_refs"] = [artifact.to_record() for artifact in self.artifact_refs]
        return record


@dataclass(frozen=True)
class ControlReceipt:
    """Operator-safe validation receipt."""

    artifact_refs: tuple[ValidationArtifactRef, ...]
    captured_at: str
    check_results: tuple[ValidationCheckResult, ...]
    digest: str
    manifest_id: str
    outcome: str
    plan_id: str
    planner_decision: dict[str, Any]
    receipt_id: str
    schema_version: int
    suppressed_output_summary: dict[str, Any]
    target_scope: str
    tier: str

    def to_record(self) -> dict[str, Any]:
        return {
            "artifact_refs": [artifact.to_record() for artifact in self.artifact_refs],
            "captured_at": self.captured_at,
            "check_results": [result.to_record() for result in self.check_results],
            "digest": self.digest,
            "manifest_id": self.manifest_id,
            "outcome": self.outcome,
            "plan_id": self.plan_id,
            "planner_decision": self.planner_decision,
            "receipt_id": self.receipt_id,
            "schema_version": self.schema_version,
            "suppressed_output_summary": self.suppressed_output_summary,
            "target_scope": self.target_scope,
            "tier": self.tier,
        }


@dataclass(frozen=True)
class LedgerEvent:
    """Append-only audit event model for validation execution."""

    action: str
    actor: str
    artifact_refs: tuple[ValidationArtifactRef, ...]
    event_id: str
    event_time: str
    outcome: str
    receipt_refs: tuple[dict[str, str], ...]
    schema_version: int
    target: str

    def to_record(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "actor": self.actor,
            "artifact_refs": [artifact.to_record() for artifact in self.artifact_refs],
            "event_id": self.event_id,
            "event_time": self.event_time,
            "outcome": self.outcome,
            "receipt_refs": list(self.receipt_refs),
            "schema_version": self.schema_version,
            "target": self.target,
        }


@dataclass(frozen=True)
class ValidationExecutionResult:
    """Execution result with receipt and ledger event records."""

    ledger_event: LedgerEvent
    receipt: ControlReceipt

    def to_record(self) -> dict[str, Any]:
        return {
            "ledger_event": self.ledger_event.to_record(),
            "receipt": self.receipt.to_record(),
        }


def execute_validation_plan(
    plan: ValidationPlan,
    repo_root: str | Path,
    artifact_root: str | Path,
    *,
    actor: str = "wgcf-local",
    env: dict[str, str] | None = None,
    now: datetime | str | None = None,
    timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
) -> ValidationExecutionResult:
    """Run command checks from a validation plan and produce a receipt."""

    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ValueError(f"repo_root does not exist or is not a directory: {root}")
    artifacts_root = Path(artifact_root).resolve()
    artifacts_root.mkdir(parents=True, exist_ok=True)

    captured_at = _coerce_timestamp(now)
    captured_at_text = captured_at.isoformat().replace("+00:00", "Z")
    check_results: list[ValidationCheckResult] = []
    artifact_refs: list[ValidationArtifactRef] = []
    if plan.decision.outcome == "planned":
        for check in plan.checks:
            if check.execution_mode == ValidationExecutionMode.SKIP_FRESH_RECEIPT.value:
                result = ValidationCheckResult(
                    artifact_refs=(),
                    check_id=check.check_id,
                    command_digest=None,
                    duration_ms=None,
                    error=None,
                    exit_code=None,
                    output_summary={
                        "reused_receipt_id": check.receipt_id,
                        "suppressed": True,
                    },
                    required=check.required,
                    reused_receipt_id=check.receipt_id,
                    status="skipped_fresh_receipt",
                    validator_id=check.validator_id,
                )
                check_results.append(result)
                continue

            if check.check_type != "command":
                result = ValidationCheckResult(
                    artifact_refs=(),
                    check_id=check.check_id,
                    command_digest=None,
                    duration_ms=None,
                    error=f"unsupported check_type {check.check_type!r}",
                    exit_code=None,
                    output_summary={"suppressed": True},
                    required=check.required,
                    reused_receipt_id=None,
                    status="blocked",
                    validator_id=check.validator_id,
                )
                check_results.append(result)
                continue

            result = _run_command_check(
                check,
                root,
                artifacts_root,
                env=env,
                timeout_seconds=timeout_seconds,
            )
            check_results.append(result)
            artifact_refs.extend(result.artifact_refs)

    outcome = _receipt_outcome(plan, check_results)
    planner_decision = plan.decision.to_record()
    suppressed_output_summary = {
        "artifact_count": len(artifact_refs),
        "execution_suppressed": plan.decision.outcome != "planned",
        "planner_outcome": plan.decision.outcome,
        "raw_output_in_receipt": False,
        "suppressed_streams": sorted(
            {
                stream
                for result in check_results
                for stream, summary in result.output_summary.items()
                if isinstance(summary, dict) and summary.get("suppressed") is True
            },
        ),
    }
    digest_payload = {
        "artifact_refs": [artifact.to_record() for artifact in artifact_refs],
        "captured_at": captured_at_text,
        "check_results": [result.to_record() for result in check_results],
        "manifest_id": plan.manifest_id,
        "outcome": outcome,
        "plan_id": plan.plan_id,
        "planner_decision": planner_decision,
        "suppressed_output_summary": suppressed_output_summary,
        "target_scope": plan.target.scope,
        "tier": plan.tier,
    }
    receipt_digest = _digest_json(digest_payload)
    receipt = ControlReceipt(
        artifact_refs=tuple(artifact_refs),
        captured_at=captured_at_text,
        check_results=tuple(check_results),
        digest=receipt_digest,
        manifest_id=plan.manifest_id,
        outcome=outcome,
        plan_id=plan.plan_id,
        planner_decision=planner_decision,
        receipt_id=f"control-receipt:{receipt_digest.removeprefix('sha256:')[:24]}",
        schema_version=RECEIPT_SCHEMA_VERSION,
        suppressed_output_summary=suppressed_output_summary,
        target_scope=plan.target.scope,
        tier=plan.tier,
    )
    ledger_event = build_validation_ledger_event(
        actor=actor,
        artifact_refs=tuple(artifact_refs),
        event_time=captured_at_text,
        receipt=receipt,
        target=plan.target.scope,
    )
    return ValidationExecutionResult(ledger_event=ledger_event, receipt=receipt)


def append_ledger_event(ledger_path: str | Path, event: LedgerEvent | dict[str, Any]) -> Path:
    """Append one ledger event as JSONL without rewriting prior events."""

    output_path = Path(ledger_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    record = event.to_record() if isinstance(event, LedgerEvent) else event
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True))
        handle.write("\n")
    return output_path


def write_control_receipt(receipt_path: str | Path, receipt: ControlReceipt | dict[str, Any]) -> Path:
    """Write one receipt JSON document without embedding raw validator output."""

    output_path = Path(receipt_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    record = receipt.to_record() if isinstance(receipt, ControlReceipt) else receipt
    output_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path


def build_validation_ledger_event(
    *,
    actor: str,
    artifact_refs: tuple[ValidationArtifactRef, ...],
    event_time: str,
    receipt: ControlReceipt,
    target: str,
) -> LedgerEvent:
    action = "validation.run.completed" if receipt.outcome in {"success", "failure"} else "validation.run.blocked"
    receipt_ref = {
        "digest": receipt.digest,
        "receipt_id": receipt.receipt_id,
    }
    digest_payload = {
        "action": action,
        "actor": actor,
        "event_time": event_time,
        "outcome": receipt.outcome,
        "receipt_refs": [receipt_ref],
        "target": target,
    }
    event_digest = _digest_json(digest_payload).removeprefix("sha256:")
    return LedgerEvent(
        action=action,
        actor=actor,
        artifact_refs=artifact_refs,
        event_id=f"ledger-event:{event_digest[:24]}",
        event_time=event_time,
        outcome=receipt.outcome,
        receipt_refs=(receipt_ref,),
        schema_version=LEDGER_SCHEMA_VERSION,
        target=target,
    )


def _run_command_check(
    check,
    repo_root: Path,
    artifact_root: Path,
    *,
    env: dict[str, str] | None,
    timeout_seconds: int,
) -> ValidationCheckResult:
    started = datetime.now(UTC)
    try:
        env_overrides, args = _parse_command(check.command)
    except ValueError as exc:
        return ValidationCheckResult(
            artifact_refs=(),
            check_id=check.check_id,
            command_digest=_digest_text(check.command),
            duration_ms=0,
            error=str(exc),
            exit_code=None,
            output_summary={"suppressed": True},
            required=check.required,
            reused_receipt_id=None,
            status="blocked",
            validator_id=check.validator_id,
        )
    command_env = dict(os.environ)
    if env:
        command_env.update(env)
    command_env.update(env_overrides)

    stdout = b""
    stderr = b""
    exit_code: int | None = None
    error: str | None = None
    timed_out = False
    try:
        completed = subprocess.run(
            args,
            cwd=repo_root,
            env=command_env,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        stdout = completed.stdout or b""
        stderr = completed.stderr or b""
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = _bytes_or_empty(exc.stdout)
        stderr = _bytes_or_empty(exc.stderr)
        error = f"command timed out after {timeout_seconds} seconds"
        timed_out = True
    except OSError as exc:
        error = str(exc)

    completed_at = datetime.now(UTC)
    duration_ms = int((completed_at - started).total_seconds() * 1000)
    check_dir = artifact_root / _safe_path_id(check.check_id)
    stdout_ref = _write_artifact(check_dir, "stdout", stdout)
    stderr_ref = _write_artifact(check_dir, "stderr", stderr)
    artifact_refs = (stdout_ref, stderr_ref)
    status = "success" if exit_code == 0 and error is None else "failure"
    output_summary = {
        "stderr": _stream_summary(stderr, stderr_ref),
        "stdout": _stream_summary(stdout, stdout_ref),
        "timed_out": timed_out,
    }
    return ValidationCheckResult(
        artifact_refs=artifact_refs,
        check_id=check.check_id,
        command_digest=_digest_text(check.command),
        duration_ms=duration_ms,
        error=error,
        exit_code=exit_code,
        output_summary=output_summary,
        required=check.required,
        reused_receipt_id=None,
        status=status,
        validator_id=check.validator_id,
    )


def _parse_command(command: str) -> tuple[dict[str, str], list[str]]:
    parts = shlex.split(command)
    env_overrides: dict[str, str] = {}
    while parts and _is_env_assignment(parts[0]):
        key, value = parts.pop(0).split("=", 1)
        env_overrides[key] = value
    if not parts:
        raise ValueError("validator command must include an executable")
    return env_overrides, parts


def _is_env_assignment(value: str) -> bool:
    if "=" not in value:
        return False
    key, _ = value.split("=", 1)
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is not None


def _write_artifact(root: Path, stream_name: str, content: bytes) -> ValidationArtifactRef:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{stream_name}.log"
    path.write_bytes(content)
    digest = _digest_bytes(content)
    artifact_identity = _digest_text(f"{root.name}:{stream_name}:{digest}")
    return ValidationArtifactRef(
        artifact_id=f"artifact:{artifact_identity.removeprefix('sha256:')[:24]}",
        byte_count=len(content),
        digest=digest,
        media_type="text/plain; charset=utf-8",
        path=str(path),
        purpose=f"validation-{stream_name}",
    )


def _stream_summary(content: bytes, artifact: ValidationArtifactRef) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "byte_count": len(content),
        "digest": artifact.digest,
        "line_count": len(content.splitlines()),
        "suppressed": True,
    }


def _receipt_outcome(plan: ValidationPlan, results: list[ValidationCheckResult]) -> str:
    if plan.decision.outcome == "blocked":
        return "blocked"
    if plan.decision.outcome != "planned":
        return "operator_review_required"
    if not results:
        return "operator_review_required"
    required_results = [result for result in results if result.required]
    if any(result.status in {"failure", "blocked"} for result in required_results):
        return "failure"
    if any(result.status == "blocked" for result in results):
        return "blocked"
    return "success"


def _coerce_timestamp(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _bytes_or_empty(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _safe_path_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "check"


def _digest_text(value: str) -> str:
    return _digest_bytes(value.encode("utf-8"))


def _digest_json(value: dict[str, Any]) -> str:
    return _digest_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()
