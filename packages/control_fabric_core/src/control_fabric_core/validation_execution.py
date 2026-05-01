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
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .performance_budgets import coerce_execution_limits
from .validation_planning import (
    ValidationExecutionMode,
    ValidationPlan,
)


RECEIPT_SCHEMA_VERSION = 1
LEDGER_SCHEMA_VERSION = 1
COMMAND_TIMEOUT_SECONDS = 120
DEFAULT_SAFETY_CLASS = "local-read-only"
DEFAULT_EXECUTION_PROFILE = "developer"
OPERATOR_APPROVAL_REQUIRED_SAFETY_CLASSES = {
    "authority-mutation",
    "host-control",
    "live-runtime-read",
    "materialized-output-write",
    "network",
    "privileged",
    "remote-read",
    "structured-record-write",
}
SUPPORTED_SAFETY_CLASSES = {
    "authority-mutation",
    "host-control",
    "live-runtime-read",
    "local-artifact-write",
    "local-read-only",
    "materialized-output-write",
    "network",
    "privileged",
    "remote-read",
    "structured-record-write",
    "workspace-cross-repo-read",
}
DEFAULT_COMMAND_ENV_NAMES = {
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "PYTHONPATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "VIRTUAL_ENV",
}
SECRET_LIKE_ENV_RE = re.compile(r"(?:TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE[_-]?KEY|API[_-]?KEY)", re.I)


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
        "custody": _artifact_custody_summary(tuple(artifact_refs)),
        "execution_suppressed": plan.decision.outcome != "planned",
        "planner_outcome": plan.decision.outcome,
        "performance_budget": getattr(plan, "performance_budget", {}),
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
        "outcome": receipt.outcome,
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
    execution_policy = _execution_policy(check)
    try:
        working_directory = _working_directory(repo_root, execution_policy)
    except ValueError as exc:
        return _blocked_check_result(
            check,
            command_digest=_digest_text(check.command),
            duration_ms=0,
            error=str(exc),
            output_summary={
                "suppressed": True,
            },
        )
    safety_block = _safety_block_reason(
        args=args,
        env_overrides=env_overrides,
        execution_policy=execution_policy,
        repo_root=repo_root,
        supplied_env=env or {},
        working_directory=working_directory,
    )
    if safety_block is not None:
        return _blocked_check_result(
            check,
            command_digest=_digest_text(check.command),
            duration_ms=0,
            error=safety_block["reason"],
            output_summary={
                "safety": safety_block,
                "suppressed": True,
            },
        )
    execution_limits = coerce_execution_limits(
        execution_policy,
        default_timeout_seconds=timeout_seconds,
        profile=str(execution_policy.get("profile") or DEFAULT_EXECUTION_PROFILE),
    )
    effective_timeout_seconds = execution_limits.timeout_seconds
    retry_count = execution_limits.retry_count
    output_budget_bytes = execution_limits.output_budget_bytes
    fail_on_budget = bool(execution_policy.get("fail_on_output_budget_exceeded", False))

    command_env = _base_command_env()
    if execution_policy.get("prefer_current_python", True) is not False:
        command_env["PATH"] = _python_first_path(command_env.get("PATH"))
    if env:
        command_env.update(env)
    command_env.update(env_overrides)

    check_dir = artifact_root / _safe_path_id(check.check_id)
    artifact_refs: list[ValidationArtifactRef] = []
    attempt_summaries: list[dict[str, Any]] = []
    final_stdout = b""
    final_stderr = b""
    final_stdout_ref: ValidationArtifactRef | None = None
    final_stderr_ref: ValidationArtifactRef | None = None
    exit_code: int | None = None
    error: str | None = None
    timed_out = False
    max_attempts = retry_count + 1
    for attempt in range(1, max_attempts + 1):
        stdout = b""
        stderr = b""
        attempt_exit_code: int | None = None
        attempt_error: str | None = None
        attempt_timed_out = False
        try:
            completed = subprocess.run(
                args,
                cwd=working_directory,
                env=command_env,
                capture_output=True,
                check=False,
                timeout=effective_timeout_seconds,
            )
            stdout = completed.stdout or b""
            stderr = completed.stderr or b""
            attempt_exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = _bytes_or_empty(exc.stdout)
            stderr = _bytes_or_empty(exc.stderr)
            attempt_error = f"command timed out after {effective_timeout_seconds} seconds"
            attempt_timed_out = True
        except OSError as exc:
            attempt_error = str(exc)

        if max_attempts > 1:
            stdout_ref = _write_artifact(check_dir, f"attempt-{attempt}-stdout", stdout)
            stderr_ref = _write_artifact(check_dir, f"attempt-{attempt}-stderr", stderr)
        else:
            stdout_ref = _write_artifact(check_dir, "stdout", stdout)
            stderr_ref = _write_artifact(check_dir, "stderr", stderr)
        artifact_refs.extend((stdout_ref, stderr_ref))
        attempt_summaries.append(
            {
                "attempt": attempt,
                "exit_code": attempt_exit_code,
                "stderr": _stream_summary(stderr, stderr_ref),
                "stdout": _stream_summary(stdout, stdout_ref),
                "timed_out": attempt_timed_out,
            },
        )
        if attempt_error:
            attempt_summaries[-1]["error"] = attempt_error

        final_stdout = stdout
        final_stderr = stderr
        final_stdout_ref = stdout_ref
        final_stderr_ref = stderr_ref
        exit_code = attempt_exit_code
        error = attempt_error
        timed_out = attempt_timed_out
        if attempt_exit_code == 0 and attempt_error is None:
            break

    completed_at = datetime.now(UTC)
    duration_ms = int((completed_at - started).total_seconds() * 1000)
    budget_decision = _output_budget_decision(
        tuple(artifact_refs),
        output_budget_bytes,
        fail_on_budget,
    )
    if budget_decision["exceeded"] and fail_on_budget:
        status = "failure"
        budget_error = (
            f"output budget exceeded: {budget_decision['observed_bytes']} bytes "
            f"over {budget_decision['budget_bytes']} byte budget"
        )
        error = f"{error}; {budget_error}" if error else budget_error
    else:
        status = "success" if exit_code == 0 and error is None else "failure"
    assert final_stdout_ref is not None
    assert final_stderr_ref is not None
    output_summary = {
        "attempt_count": len(attempt_summaries),
        "attempts": attempt_summaries,
        "output_budget": budget_decision,
        "performance_budget": execution_limits.to_record(),
        "retry_count": retry_count,
        "safety": _safety_summary(args=args, execution_policy=execution_policy),
        "stderr": _stream_summary(final_stderr, final_stderr_ref),
        "stdout": _stream_summary(final_stdout, final_stdout_ref),
        "timed_out": timed_out,
        "timeout_seconds": effective_timeout_seconds,
    }
    return ValidationCheckResult(
        artifact_refs=tuple(artifact_refs),
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


def _blocked_check_result(
    check,
    *,
    command_digest: str | None,
    duration_ms: int | None,
    error: str,
    output_summary: dict[str, Any],
) -> ValidationCheckResult:
    return ValidationCheckResult(
        artifact_refs=(),
        check_id=check.check_id,
        command_digest=command_digest,
        duration_ms=duration_ms,
        error=error,
        exit_code=None,
        output_summary=output_summary,
        required=check.required,
        reused_receipt_id=None,
        status="blocked",
        validator_id=check.validator_id,
    )


def _execution_policy(check) -> dict[str, Any]:
    policy = getattr(check, "execution_policy", {})
    return policy if isinstance(policy, dict) else {}


def _output_budget_decision(
    artifact_refs: tuple[ValidationArtifactRef, ...],
    output_budget_bytes: int | None,
    fail_on_budget: bool,
) -> dict[str, Any]:
    observed_bytes = sum(artifact.byte_count for artifact in artifact_refs)
    exceeded = output_budget_bytes is not None and observed_bytes > output_budget_bytes
    return {
        "action": "fail" if exceeded and fail_on_budget else "record_only",
        "budget_bytes": output_budget_bytes,
        "exceeded": exceeded,
        "observed_bytes": observed_bytes,
    }


def _artifact_custody_summary(artifact_refs: tuple[ValidationArtifactRef, ...]) -> dict[str, Any]:
    artifact_records = [artifact.to_record() for artifact in artifact_refs]
    return {
        "artifact_digest_manifest": _digest_json({"artifact_refs": artifact_records}),
        "artifact_ids": [artifact.artifact_id for artifact in artifact_refs],
        "artifact_purposes": sorted({artifact.purpose for artifact in artifact_refs}),
        "raw_artifacts_embedded": False,
    }


def _safety_block_reason(
    *,
    args: list[str],
    env_overrides: dict[str, str],
    execution_policy: dict[str, Any],
    repo_root: Path,
    supplied_env: dict[str, str],
    working_directory: Path,
) -> dict[str, Any] | None:
    safety_class = _safety_class(execution_policy)
    profile = _execution_profile(execution_policy)
    summary = _safety_summary(args=args, execution_policy=execution_policy)
    if safety_class not in SUPPORTED_SAFETY_CLASSES:
        return {
            **summary,
            "decision": "blocked",
            "reason": f"unsupported safety class {safety_class!r}",
        }
    if (
        safety_class in OPERATOR_APPROVAL_REQUIRED_SAFETY_CLASSES
        and not bool(execution_policy.get("operator_approved", False))
    ):
        return {
            **summary,
            "decision": "blocked",
            "reason": f"safety class {safety_class!r} requires explicit operator approval",
        }
    allowed_executables = _string_set(execution_policy.get("allowed_executables"))
    if allowed_executables and not _executable_allowed(args[0], allowed_executables):
        return {
            **summary,
            "decision": "blocked",
            "reason": f"executable {args[0]!r} is not in the command allowlist for profile {profile!r}",
        }
    root_decision = _allowed_root_decision(repo_root, working_directory, execution_policy)
    if root_decision is not None:
        return {
            **summary,
            "decision": "blocked",
            "reason": root_decision,
        }
    env_decision = _env_decision({**supplied_env, **env_overrides}, execution_policy)
    if env_decision is not None:
        return {
            **summary,
            "decision": "blocked",
            "reason": env_decision,
        }
    return None


def _safety_summary(*, args: list[str], execution_policy: dict[str, Any]) -> dict[str, Any]:
    allowed_env_vars = sorted(_string_set(execution_policy.get("allowed_env_vars")))
    blocked_env_vars = sorted(_string_set(execution_policy.get("blocked_env_vars")))
    return {
        "allowed_env_vars": allowed_env_vars,
        "allowed_executables": sorted(_string_set(execution_policy.get("allowed_executables"))),
        "allowed_roots": sorted(_string_set(execution_policy.get("allowed_roots"))),
        "blocked_env_vars": blocked_env_vars,
        "decision": "allowed",
        "executable": args[0] if args else None,
        "invocation_class": str(execution_policy.get("invocation_class") or "hard-gate"),
        "max_duration_ms": execution_policy.get("max_duration_ms"),
        "profile": _execution_profile(execution_policy),
        "safety_class": _safety_class(execution_policy),
        "sanitized_base_env": True,
        "working_directory": str(execution_policy.get("working_directory") or "."),
    }


def _safety_class(execution_policy: dict[str, Any]) -> str:
    return str(execution_policy.get("safety_class") or DEFAULT_SAFETY_CLASS).strip()


def _execution_profile(execution_policy: dict[str, Any]) -> str:
    return str(execution_policy.get("profile") or DEFAULT_EXECUTION_PROFILE).strip()


def _string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _executable_allowed(executable: str, allowed_executables: set[str]) -> bool:
    executable_name = Path(executable).name
    return executable in allowed_executables or executable_name in allowed_executables


def _allowed_root_decision(
    repo_root: Path,
    working_directory: Path,
    execution_policy: dict[str, Any],
) -> str | None:
    allowed_roots = _string_set(execution_policy.get("allowed_roots"))
    if not allowed_roots:
        return None
    resolved_roots = {_resolve_policy_root(repo_root, root) for root in allowed_roots}
    if any(_is_relative_to(working_directory, allowed_root) for allowed_root in resolved_roots):
        return None
    return "validator working directory is outside the allowed_roots policy"


def _working_directory(repo_root: Path, execution_policy: dict[str, Any]) -> Path:
    raw_value = execution_policy.get("working_directory") or "."
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ValueError("execution_policy.working_directory must be a non-empty string")
    candidate = Path(raw_value.strip())
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    if not _is_relative_to(resolved, repo_root):
        raise ValueError("execution_policy.working_directory must stay inside repo_root")
    if not resolved.is_dir():
        raise ValueError(f"execution_policy.working_directory does not exist: {resolved}")
    return resolved


def _resolve_policy_root(repo_root: Path, root: str) -> Path:
    candidate = Path(root)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate.resolve()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _env_decision(env_overrides: dict[str, str], execution_policy: dict[str, Any]) -> str | None:
    if not env_overrides:
        return None
    allowed_env_vars = _string_set(execution_policy.get("allowed_env_vars"))
    blocked_env_vars = _string_set(execution_policy.get("blocked_env_vars"))
    for key in sorted(env_overrides):
        if key in blocked_env_vars:
            return f"environment variable {key!r} is explicitly blocked"
        if allowed_env_vars and key not in allowed_env_vars:
            return f"environment variable {key!r} is not in the allowed_env_vars policy"
        if SECRET_LIKE_ENV_RE.search(key) and key not in allowed_env_vars:
            return f"secret-like environment variable {key!r} requires explicit allowlist"
    return None


def _base_command_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key in DEFAULT_COMMAND_ENV_NAMES and value
    }


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


def _python_first_path(current_path: str | None) -> str:
    """Prefer the interpreter running WGCF when manifest commands use python3."""

    python_bin = str(Path(sys.executable).parent)
    if not current_path:
        return python_bin
    parts = current_path.split(os.pathsep)
    if python_bin in parts:
        parts.remove(python_bin)
    return os.pathsep.join([python_bin, *parts])


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
