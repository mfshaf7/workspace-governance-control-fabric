"""Local retention, artifact cleanup, and ledger compaction controls.

The lifecycle surface manages only fabric-local WGCF state. It does not mutate
upstream authority stores, ART records, platform state, or security posture.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from .validation_execution import (
    LEDGER_SCHEMA_VERSION,
    ValidationArtifactRef,
    append_ledger_event,
)


DEFAULT_LEDGER_EXPORT_DIR = ".wgcf/ledger-exports"
DEFAULT_RETENTION_PROFILE = "developer"
RETENTION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RetentionThresholds:
    """Concrete retention limits for one operator profile."""

    artifact_max_age_days: int | None
    artifact_max_count: int
    ledger_max_events: int
    profile: str
    receipt_max_age_days: int | None
    receipt_max_count: int
    require_ledger_export_before_compaction: bool = True

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetentionCandidate:
    """One local file that can be removed by an explicit retention apply."""

    action: str
    byte_count: int
    category: str
    digest: str
    modified_at: str
    path: str
    reason: str
    relative_path: str
    safe_to_apply: bool

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LedgerCompactionPlan:
    """Plan for exporting old ledger lines and retaining the latest events."""

    action_required: bool
    compacted_line_count: int
    export_path: str | None
    export_required: bool
    ledger_path: str
    reason: str | None
    retained_line_count: int
    source_digest: str | None
    source_line_count: int

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RetentionPlan:
    """Dry-run lifecycle plan for fabric-local cleanup."""

    artifact_candidates: tuple[RetentionCandidate, ...]
    artifact_root: str
    generated_at: str
    ledger_compaction: LedgerCompactionPlan
    ledger_path: str
    plan_digest: str
    plan_id: str
    profile: str
    receipt_candidates: tuple[RetentionCandidate, ...]
    receipt_dir: str
    repo_root: str
    requires_confirmation: bool
    schema_version: int
    summary: dict[str, Any]
    thresholds: RetentionThresholds

    def to_record(self) -> dict[str, Any]:
        return {
            "artifact_candidates": [candidate.to_record() for candidate in self.artifact_candidates],
            "artifact_root": self.artifact_root,
            "generated_at": self.generated_at,
            "ledger_compaction": self.ledger_compaction.to_record(),
            "ledger_path": self.ledger_path,
            "plan_digest": self.plan_digest,
            "plan_id": self.plan_id,
            "profile": self.profile,
            "receipt_candidates": [candidate.to_record() for candidate in self.receipt_candidates],
            "receipt_dir": self.receipt_dir,
            "repo_root": self.repo_root,
            "requires_confirmation": self.requires_confirmation,
            "schema_version": self.schema_version,
            "summary": self.summary,
            "thresholds": self.thresholds.to_record(),
        }


@dataclass(frozen=True)
class RetentionApplyResult:
    """Mutation result for an explicitly confirmed retention apply."""

    applied_at: str
    deleted_artifacts: tuple[RetentionCandidate, ...]
    deleted_receipts: tuple[RetentionCandidate, ...]
    errors: tuple[str, ...]
    ledger_event: dict[str, Any] | None
    ledger_event_path: str | None
    ledger_export_ref: ValidationArtifactRef | None
    outcome: str
    plan: RetentionPlan

    def to_record(self) -> dict[str, Any]:
        return {
            "applied_at": self.applied_at,
            "deleted_artifacts": [candidate.to_record() for candidate in self.deleted_artifacts],
            "deleted_receipts": [candidate.to_record() for candidate in self.deleted_receipts],
            "errors": list(self.errors),
            "ledger_event": self.ledger_event,
            "ledger_event_path": self.ledger_event_path,
            "ledger_export_ref": self.ledger_export_ref.to_record() if self.ledger_export_ref else None,
            "outcome": self.outcome,
            "plan": self.plan.to_record(),
        }


RETENTION_PROFILES: dict[str, RetentionThresholds] = {
    "developer": RetentionThresholds(
        artifact_max_age_days=14,
        artifact_max_count=500,
        ledger_max_events=5000,
        profile="developer",
        receipt_max_age_days=30,
        receipt_max_count=500,
    ),
    "ci": RetentionThresholds(
        artifact_max_age_days=7,
        artifact_max_count=200,
        ledger_max_events=1000,
        profile="ci",
        receipt_max_age_days=14,
        receipt_max_count=200,
    ),
    "enterprise": RetentionThresholds(
        artifact_max_age_days=90,
        artifact_max_count=10000,
        ledger_max_events=50000,
        profile="enterprise",
        receipt_max_age_days=365,
        receipt_max_count=10000,
    ),
}


def retention_thresholds(profile: str = DEFAULT_RETENTION_PROFILE) -> RetentionThresholds:
    """Return a known local retention profile."""

    normalized = str(profile or DEFAULT_RETENTION_PROFILE).strip().lower()
    try:
        return RETENTION_PROFILES[normalized]
    except KeyError as exc:
        known = ", ".join(sorted(RETENTION_PROFILES))
        raise ValueError(f"unknown retention profile {profile!r}; expected one of: {known}") from exc


def build_retention_plan(
    *,
    repo_root: str | Path,
    artifact_root: str | Path,
    receipt_dir: str | Path,
    ledger_path: str | Path,
    export_dir: str | Path = DEFAULT_LEDGER_EXPORT_DIR,
    profile: str = DEFAULT_RETENTION_PROFILE,
    now: datetime | str | None = None,
) -> RetentionPlan:
    """Build a dry-run plan for local WGCF lifecycle cleanup."""

    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ValueError(f"repo_root does not exist or is not a directory: {root}")
    now_dt = _coerce_timestamp(now)
    generated_at = now_dt.isoformat().replace("+00:00", "Z")
    thresholds = retention_thresholds(profile)
    artifact_path = _resolve_local_path(root, artifact_root, "artifact_root")
    receipt_path = _resolve_local_path(root, receipt_dir, "receipt_dir")
    ledger_file = _resolve_local_path(root, ledger_path, "ledger_path")
    export_path = _resolve_local_path(root, export_dir, "export_dir")

    artifact_candidates = _retention_candidates(
        root=root,
        category="artifact",
        action="delete",
        max_age_days=thresholds.artifact_max_age_days,
        max_count=thresholds.artifact_max_count,
        now=now_dt,
        scan_root=artifact_path,
    )
    receipt_candidates = _retention_candidates(
        root=root,
        category="receipt",
        action="delete",
        max_age_days=thresholds.receipt_max_age_days,
        max_count=thresholds.receipt_max_count,
        now=now_dt,
        scan_root=receipt_path,
        pattern="*.json",
    )
    ledger_compaction = _build_ledger_compaction_plan(
        generated_at=generated_at,
        ledger_path=ledger_file,
        export_dir=export_path,
        max_events=thresholds.ledger_max_events,
    )
    summary = {
        "apply_appends_ledger_event": True,
        "artifact_delete_count": len(artifact_candidates),
        "artifact_delete_bytes": sum(candidate.byte_count for candidate in artifact_candidates),
        "ledger_compaction_required": ledger_compaction.action_required,
        "ledger_compacted_line_count": ledger_compaction.compacted_line_count,
        "mutation_count": len(artifact_candidates) + len(receipt_candidates) + int(ledger_compaction.action_required),
        "receipt_delete_count": len(receipt_candidates),
        "receipt_delete_bytes": sum(candidate.byte_count for candidate in receipt_candidates),
    }
    digest_payload = {
        "artifact_candidates": [candidate.to_record() for candidate in artifact_candidates],
        "artifact_root": str(artifact_path),
        "generated_at": generated_at,
        "ledger_compaction": ledger_compaction.to_record(),
        "ledger_path": str(ledger_file),
        "profile": thresholds.profile,
        "receipt_candidates": [candidate.to_record() for candidate in receipt_candidates],
        "receipt_dir": str(receipt_path),
        "summary": summary,
        "thresholds": thresholds.to_record(),
    }
    plan_digest = _digest_json(digest_payload)
    return RetentionPlan(
        artifact_candidates=artifact_candidates,
        artifact_root=str(artifact_path),
        generated_at=generated_at,
        ledger_compaction=ledger_compaction,
        ledger_path=str(ledger_file),
        plan_digest=plan_digest,
        plan_id=f"retention-plan:{plan_digest.removeprefix('sha256:')[:24]}",
        profile=thresholds.profile,
        receipt_candidates=receipt_candidates,
        receipt_dir=str(receipt_path),
        repo_root=str(root),
        requires_confirmation=True,
        schema_version=RETENTION_SCHEMA_VERSION,
        summary=summary,
        thresholds=thresholds,
    )


def apply_retention_plan(
    *,
    repo_root: str | Path,
    artifact_root: str | Path,
    receipt_dir: str | Path,
    ledger_path: str | Path,
    actor: str = "wgcf-local",
    confirm: bool = False,
    export_dir: str | Path = DEFAULT_LEDGER_EXPORT_DIR,
    profile: str = DEFAULT_RETENTION_PROFILE,
    now: datetime | str | None = None,
) -> RetentionApplyResult:
    """Apply a retention plan only when explicitly confirmed."""

    applied_at = _coerce_timestamp(now).isoformat().replace("+00:00", "Z")
    plan = build_retention_plan(
        artifact_root=artifact_root,
        export_dir=export_dir,
        ledger_path=ledger_path,
        now=now,
        profile=profile,
        receipt_dir=receipt_dir,
        repo_root=repo_root,
    )
    if not confirm:
        return RetentionApplyResult(
            applied_at=applied_at,
            deleted_artifacts=(),
            deleted_receipts=(),
            errors=("explicit confirmation is required before applying retention cleanup",),
            ledger_event=None,
            ledger_event_path=None,
            ledger_export_ref=None,
            outcome="blocked",
            plan=plan,
        )

    errors: list[str] = []
    ledger_export_ref: ValidationArtifactRef | None = None
    if plan.ledger_compaction.action_required:
        try:
            ledger_export_ref = _compact_ledger(plan.ledger_compaction)
        except OSError as exc:
            errors.append(f"ledger compaction failed: {exc}")

    if errors:
        deleted_artifacts = ()
        deleted_receipts = ()
    else:
        deleted_artifacts = _delete_candidates(plan.artifact_candidates, errors)
        deleted_receipts = _delete_candidates(plan.receipt_candidates, errors)
    outcome = "success" if not errors else "failure"
    ledger_event = _build_lifecycle_ledger_event(
        actor=actor,
        artifact_refs=(ledger_export_ref,) if ledger_export_ref else (),
        event_time=applied_at,
        outcome=outcome,
        plan=plan,
        target="repo:workspace-governance-control-fabric",
    )
    ledger_event_path = str(append_ledger_event(plan.ledger_path, ledger_event))
    return RetentionApplyResult(
        applied_at=applied_at,
        deleted_artifacts=deleted_artifacts,
        deleted_receipts=deleted_receipts,
        errors=tuple(errors),
        ledger_event=ledger_event,
        ledger_event_path=ledger_event_path,
        ledger_export_ref=ledger_export_ref,
        outcome=outcome,
        plan=plan,
    )


def _retention_candidates(
    *,
    root: Path,
    category: str,
    action: str,
    max_age_days: int | None,
    max_count: int,
    now: datetime,
    scan_root: Path,
    pattern: str = "*",
) -> tuple[RetentionCandidate, ...]:
    if max_count < 0:
        raise ValueError("retention max_count must be zero or greater")
    if not scan_root.exists():
        return ()
    if not scan_root.is_dir():
        raise ValueError(f"{category} retention path is not a directory: {scan_root}")

    files = [
        path
        for path in scan_root.rglob(pattern)
        if path.is_file() and _is_relative_to(path.resolve(), root)
    ]
    files.sort(key=lambda item: (item.stat().st_mtime, str(item)))
    reasons_by_path: dict[Path, set[str]] = {}
    if max_age_days is not None:
        cutoff = now - timedelta(days=max_age_days)
        for path in files:
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if modified_at < cutoff:
                reasons_by_path.setdefault(path, set()).add(f"older-than-{max_age_days}-days")
    if len(files) > max_count:
        for path in files[: len(files) - max_count]:
            reasons_by_path.setdefault(path, set()).add(f"exceeds-{max_count}-file-retention-count")

    candidates = [
        _candidate_from_path(
            action=action,
            category=category,
            path=path,
            reason=", ".join(sorted(reasons)),
            root=root,
        )
        for path, reasons in reasons_by_path.items()
    ]
    return tuple(sorted(candidates, key=lambda item: (item.modified_at, item.relative_path)))


def _candidate_from_path(
    *,
    action: str,
    category: str,
    path: Path,
    reason: str,
    root: Path,
) -> RetentionCandidate:
    stat = path.stat()
    return RetentionCandidate(
        action=action,
        byte_count=stat.st_size,
        category=category,
        digest=_digest_file(path),
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat().replace("+00:00", "Z"),
        path=str(path.resolve()),
        reason=reason,
        relative_path=str(path.resolve().relative_to(root)),
        safe_to_apply=_is_relative_to(path.resolve(), root),
    )


def _build_ledger_compaction_plan(
    *,
    generated_at: str,
    ledger_path: Path,
    export_dir: Path,
    max_events: int,
) -> LedgerCompactionPlan:
    if max_events < 1:
        raise ValueError("ledger_max_events must be at least one")
    if not ledger_path.exists():
        return LedgerCompactionPlan(
            action_required=False,
            compacted_line_count=0,
            export_path=None,
            export_required=False,
            ledger_path=str(ledger_path),
            reason=None,
            retained_line_count=0,
            source_digest=None,
            source_line_count=0,
        )
    if not ledger_path.is_file():
        raise ValueError(f"ledger path is not a file: {ledger_path}")
    source_bytes = ledger_path.read_bytes()
    source_lines = source_bytes.splitlines(keepends=True)
    source_line_count = len(source_lines)
    if source_line_count <= max_events:
        return LedgerCompactionPlan(
            action_required=False,
            compacted_line_count=0,
            export_path=None,
            export_required=False,
            ledger_path=str(ledger_path),
            reason=None,
            retained_line_count=source_line_count,
            source_digest=_digest_bytes(source_bytes),
            source_line_count=source_line_count,
        )
    retained_line_count = max(0, max_events - 1)
    compacted_line_count = source_line_count - retained_line_count
    source_digest = _digest_bytes(source_bytes)
    export_name = (
        f"ledger-export-{generated_at.replace(':', '').replace('-', '')}-"
        f"{source_digest.removeprefix('sha256:')[:16]}.jsonl"
    )
    return LedgerCompactionPlan(
        action_required=True,
        compacted_line_count=compacted_line_count,
        export_path=str(export_dir / export_name),
        export_required=True,
        ledger_path=str(ledger_path),
        reason=f"ledger exceeds {max_events} event retention budget",
        retained_line_count=retained_line_count,
        source_digest=source_digest,
        source_line_count=source_line_count,
    )


def _compact_ledger(plan: LedgerCompactionPlan) -> ValidationArtifactRef:
    ledger_path = Path(plan.ledger_path)
    export_path = Path(plan.export_path or "")
    if not plan.action_required or not plan.export_path:
        raise ValueError("ledger compaction was not planned")
    source_lines = ledger_path.read_bytes().splitlines(keepends=True)
    compacted_lines = source_lines[: plan.compacted_line_count]
    retained_lines = source_lines[plan.compacted_line_count :]
    export_path.parent.mkdir(parents=True, exist_ok=True)
    export_bytes = b"".join(compacted_lines)
    export_path.write_bytes(export_bytes)
    ledger_path.write_bytes(b"".join(retained_lines))
    return ValidationArtifactRef(
        artifact_id=f"artifact:{_digest_bytes(export_bytes).removeprefix('sha256:')[:24]}",
        byte_count=len(export_bytes),
        digest=_digest_bytes(export_bytes),
        media_type="application/x-ndjson",
        path=str(export_path),
        purpose="ledger-compaction-export",
    )


def _delete_candidates(
    candidates: tuple[RetentionCandidate, ...],
    errors: list[str],
) -> tuple[RetentionCandidate, ...]:
    deleted: list[RetentionCandidate] = []
    for candidate in candidates:
        if not candidate.safe_to_apply:
            errors.append(f"unsafe retention candidate skipped: {candidate.path}")
            continue
        try:
            Path(candidate.path).unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"failed to delete {candidate.relative_path}: {exc}")
            continue
        deleted.append(candidate)
    return tuple(deleted)


def _build_lifecycle_ledger_event(
    *,
    actor: str,
    artifact_refs: tuple[ValidationArtifactRef, ...],
    event_time: str,
    outcome: str,
    plan: RetentionPlan,
    target: str,
) -> dict[str, Any]:
    digest_payload = {
        "action": "lifecycle.retention.applied",
        "actor": actor,
        "event_time": event_time,
        "outcome": outcome,
        "plan_digest": plan.plan_digest,
        "plan_id": plan.plan_id,
        "target": target,
    }
    return {
        "action": "lifecycle.retention.applied",
        "actor": actor,
        "artifact_refs": [artifact.to_record() for artifact in artifact_refs],
        "event_id": f"ledger-event:{_digest_json(digest_payload).removeprefix('sha256:')[:24]}",
        "event_time": event_time,
        "outcome": outcome,
        "record_refs": [
            {
                "digest": plan.plan_digest,
                "outcome": outcome,
                "record_id": plan.plan_id,
                "record_type": "retention-plan",
            },
        ],
        "receipt_refs": [],
        "schema_version": LEDGER_SCHEMA_VERSION,
        "target": target,
    }


def _coerce_timestamp(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).astimezone(UTC)


def _resolve_local_path(repo_root: Path, value: str | Path, label: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    if not _is_relative_to(resolved, repo_root):
        raise ValueError(f"{label} must stay inside the repository root")
    return resolved


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _digest_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + sha256(payload).hexdigest()


def _digest_json(payload: dict[str, Any]) -> str:
    return _digest_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
