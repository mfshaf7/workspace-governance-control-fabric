"""Operator-facing validation and receipt helpers.

These helpers keep CLI and API surfaces compact. Raw validator output remains
in artifact files referenced by receipts; operator responses return summaries
and paths only.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .foundation import AUTHORITY_CONTRACT_REF, RUNTIME_REPO, status_snapshot
from .graph_queries import load_governance_manifest_file
from .catalog_manifest import (
    CatalogManifestResult,
    build_catalog_governance_manifest,
)
from .observability import (
    build_correlation_id,
    operator_readiness_metrics,
    receipt_metrics_snapshot,
)
from .validation_execution import (
    LEDGER_SCHEMA_VERSION,
    ControlReceipt,
    LedgerEvent,
    append_ledger_event,
    execute_validation_plan,
    write_control_receipt,
)
from .validation_planning import ValidationPlan, build_validation_plan


DEFAULT_ARTIFACT_ROOT = ".wgcf/artifacts"
DEFAULT_LEDGER_PATH = ".wgcf/ledger.jsonl"
DEFAULT_RECEIPT_DIR = ".wgcf/receipts"
SUPPORTED_OPERATOR_PROFILES = ("local-read-only", "dev-integration", "governed-stage")
KNOWN_OPERATOR_SURFACE_IDS = ("wgcf-cli",)


@dataclass(frozen=True)
class OperatorCheckResult:
    """Compact result returned after running one operator validation check."""

    artifact_root: str
    ledger_event: LedgerEvent
    ledger_path: str
    manifest_path: str
    plan: ValidationPlan
    receipt: ControlReceipt
    receipt_path: str

    def to_record(self) -> dict[str, Any]:
        return {
            "artifact_root": self.artifact_root,
            "ledger_event": self.ledger_event.to_record(),
            "ledger_path": self.ledger_path,
            "manifest_path": self.manifest_path,
            "plan": self.plan.to_record(),
            "receipt": self.receipt.to_record(),
            "receipt_path": self.receipt_path,
        }


@dataclass(frozen=True)
class CatalogOperatorPlanResult:
    """Catalog-backed validation plan result."""

    catalog: CatalogManifestResult
    plan: ValidationPlan

    def to_record(self) -> dict[str, Any]:
        return {
            "catalog": _catalog_summary_for_plan(self.catalog, self.plan),
            "plan": self.plan.to_record(),
        }


@dataclass(frozen=True)
class CatalogOperatorCheckResult:
    """Catalog-backed validation check result."""

    catalog: CatalogManifestResult
    check_result: OperatorCheckResult
    manifest_path: str

    @property
    def receipt(self) -> ControlReceipt:
        return self.check_result.receipt

    def to_record(self) -> dict[str, Any]:
        record = self.check_result.to_record()
        record["catalog"] = _catalog_summary_for_plan(
            self.catalog,
            self.check_result.plan,
        )
        record["catalog_manifest_path"] = self.manifest_path
        return record


@dataclass(frozen=True)
class ReceiptSummary:
    """Operator-safe receipt metadata for list views."""

    artifact_count: int
    captured_at: str
    check_count: int
    correlation_id: str | None
    digest: str
    metrics: dict[str, Any]
    outcome: str
    path: str
    receipt_id: str
    target_scope: str
    tier: str

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReceiptInspection:
    """Operator-safe detailed view of one receipt."""

    artifact_count: int
    check_status_counts: dict[str, int]
    next_action: str
    raw_output_embedded: bool
    receipt: dict[str, Any]
    receipt_path: str

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OperatorReadinessDecision:
    """Compact readiness decision for one operator target and profile."""

    authority_refs: tuple[str, ...]
    correlation_id: str
    decision_id: str
    escalation_target: str | None
    mutation_boundary: str
    metrics: dict[str, Any]
    outcome: str
    profile: str
    ready: bool
    reasons: tuple[str, ...]
    receipt_refs: tuple[dict[str, str], ...]
    target: str

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["authority_refs"] = list(self.authority_refs)
        record["reasons"] = list(self.reasons)
        record["receipt_refs"] = list(self.receipt_refs)
        return record


@dataclass(frozen=True)
class OperatorReadinessResult:
    """Readiness decision plus the local ledger append result."""

    decision: OperatorReadinessDecision
    ledger_event: LedgerEvent
    ledger_path: str

    def to_record(self) -> dict[str, Any]:
        record = self.decision.to_record()
        record["ledger_event"] = self.ledger_event.to_record()
        record["ledger_path"] = self.ledger_path
        return record


def build_operator_validation_plan(
    manifest_path: str | Path,
    target_scope: str,
    *,
    tier: str = "scoped",
) -> ValidationPlan:
    """Build an operator-safe validation plan from a local manifest file."""

    manifest = load_governance_manifest_file(manifest_path)
    return build_validation_plan(manifest, target_scope, tier=tier)


def build_catalog_operator_validation_plan(
    *,
    workspace_root: str | Path,
    target_scope: str,
    catalog_path: str | Path | None = None,
    operator_approved: bool = False,
    profile: str = "local-read-only",
    tier: str = "scoped",
) -> CatalogOperatorPlanResult:
    """Build a validation plan from the workspace-owned validator catalog."""

    catalog = build_catalog_governance_manifest(
        catalog_path=catalog_path,
        operator_approved=operator_approved,
        profile=profile,
        workspace_root=workspace_root,
    )
    plan = build_validation_plan(catalog.manifest, target_scope, tier=tier)
    return CatalogOperatorPlanResult(catalog=catalog, plan=plan)


def run_operator_validation_check(
    *,
    manifest_path: str | Path,
    target_scope: str,
    repo_root: str | Path,
    tier: str = "scoped",
    artifact_root: str | Path = DEFAULT_ARTIFACT_ROOT,
    receipt_dir: str | Path = DEFAULT_RECEIPT_DIR,
    ledger_path: str | Path = DEFAULT_LEDGER_PATH,
    actor: str = "wgcf-local",
) -> OperatorCheckResult:
    """Plan, execute, write receipt, and append a ledger event."""

    root = Path(repo_root).resolve()
    artifact_path = _resolve_local_path(root, artifact_root)
    receipt_path_root = _resolve_local_path(root, receipt_dir)
    ledger_file = _resolve_local_path(root, ledger_path)
    plan = build_operator_validation_plan(manifest_path, target_scope, tier=tier)
    execution = execute_validation_plan(
        plan,
        repo_root=root,
        artifact_root=artifact_path,
        actor=actor,
    )
    receipt_output = receipt_path_root / f"{_safe_file_stem(execution.receipt.receipt_id)}.json"
    receipt_path = write_control_receipt(receipt_output, execution.receipt)
    event_path = append_ledger_event(ledger_file, execution.ledger_event)
    return OperatorCheckResult(
        artifact_root=str(artifact_path),
        ledger_event=execution.ledger_event,
        ledger_path=str(event_path),
        manifest_path=str(Path(manifest_path)),
        plan=plan,
        receipt=execution.receipt,
        receipt_path=str(receipt_path),
    )


def run_catalog_operator_validation_check(
    *,
    workspace_root: str | Path,
    target_scope: str,
    artifact_root: str | Path,
    receipt_dir: str | Path,
    ledger_path: str | Path,
    manifest_dir: str | Path,
    actor: str = "wgcf-local",
    catalog_path: str | Path | None = None,
    operator_approved: bool = False,
    profile: str = "local-read-only",
    tier: str = "scoped",
) -> CatalogOperatorCheckResult:
    """Run catalog-authorized checks and emit compact receipt evidence."""

    plan_result = build_catalog_operator_validation_plan(
        catalog_path=catalog_path,
        operator_approved=operator_approved,
        profile=profile,
        target_scope=target_scope,
        tier=tier,
        workspace_root=workspace_root,
    )
    manifest_root = Path(manifest_dir)
    manifest_root.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_root / f"{_safe_file_stem(plan_result.catalog.manifest['manifest_id'])}.json"
    manifest_path.write_text(
        json.dumps(plan_result.catalog.manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    execution = execute_validation_plan(
        plan_result.plan,
        repo_root=Path(workspace_root).resolve(),
        artifact_root=artifact_root,
        actor=actor,
    )
    receipt_output = Path(receipt_dir) / f"{_safe_file_stem(execution.receipt.receipt_id)}.json"
    receipt_path = write_control_receipt(receipt_output, execution.receipt)
    event_path = append_ledger_event(ledger_path, execution.ledger_event)
    check_result = OperatorCheckResult(
        artifact_root=str(Path(artifact_root)),
        ledger_event=execution.ledger_event,
        ledger_path=str(event_path),
        manifest_path=str(manifest_path),
        plan=plan_result.plan,
        receipt=execution.receipt,
        receipt_path=str(receipt_path),
    )
    return CatalogOperatorCheckResult(
        catalog=plan_result.catalog,
        check_result=check_result,
        manifest_path=str(manifest_path),
    )


def list_control_receipts(receipt_dir: str | Path) -> tuple[ReceiptSummary, ...]:
    """Return compact metadata for local receipt JSON documents."""

    root = Path(receipt_dir)
    if not root.exists():
        return ()
    if not root.is_dir():
        raise ValueError(f"receipt_dir is not a directory: {root}")

    summaries = [
        _receipt_summary(path)
        for path in sorted(root.glob("*.json"))
        if path.is_file()
    ]
    return tuple(
        sorted(
            summaries,
            key=lambda item: (item.captured_at, item.receipt_id),
            reverse=True,
        ),
    )


def inspect_control_receipt(
    receipt_ref: str | Path,
    *,
    receipt_dir: str | Path = DEFAULT_RECEIPT_DIR,
) -> ReceiptInspection:
    """Inspect one compact receipt without reading raw artifact output."""

    path = _resolve_receipt_ref(receipt_ref, receipt_dir)
    record = _load_receipt_record(path)
    required = ("captured_at", "digest", "outcome", "receipt_id", "target_scope", "tier")
    missing = [field for field in required if not str(record.get(field) or "").strip()]
    if missing:
        raise ValueError(f"receipt {path} missing required fields: {', '.join(missing)}")
    artifact_refs = record.get("artifact_refs") or []
    check_results = record.get("check_results") or []
    if not isinstance(artifact_refs, list):
        raise ValueError(f"receipt {path} artifact_refs must be an array")
    if not isinstance(check_results, list):
        raise ValueError(f"receipt {path} check_results must be an array")

    status_counts: dict[str, int] = {}
    for result in check_results:
        if not isinstance(result, dict):
            continue
        status = str(result.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    outcome = str(record.get("outcome") or "unknown")
    return ReceiptInspection(
        artifact_count=len(artifact_refs),
        check_status_counts=dict(sorted(status_counts.items())),
        next_action=(
            "Use receipt and artifact references as compact evidence."
            if outcome == "success"
            else "Inspect referenced artifacts locally and route the failing control through its owner."
        ),
        raw_output_embedded=False,
        receipt=record,
        receipt_path=str(path),
    )


def evaluate_operator_readiness(
    *,
    profile: str,
    receipt_dir: str | Path = DEFAULT_RECEIPT_DIR,
    repo_root: str | Path,
    target: str,
) -> OperatorReadinessDecision:
    """Evaluate local operator-readiness posture without mutating authority stores."""

    root = Path(repo_root).resolve()
    target_value = str(target or "").strip()
    profile_value = str(profile or "").strip()
    reasons: list[str] = []
    if not target_value:
        reasons.append("target is required")
    if not profile_value:
        reasons.append("profile is required")
    elif profile_value not in SUPPORTED_OPERATOR_PROFILES:
        reasons.append(
            f"unknown profile: {profile_value}; expected one of {', '.join(SUPPORTED_OPERATOR_PROFILES)}",
        )
    if target_value and not _target_supported(target_value):
        reasons.append(
            "target must be workspace, repo:<name>, component:<name>, or operator-surface:<id>",
        )

    snapshot = status_snapshot(root)
    target_authority_reasons = _target_authority_reasons(root, target_value)
    reasons.extend(target_authority_reasons)
    missing_paths = [
        path
        for path, present in sorted(snapshot["required_paths"].items())
        if not present
    ]
    for missing in missing_paths:
        reasons.append(f"required path missing: {missing}")

    if target_value.startswith("operator-surface:") and not (
        root / "docs/operations/operator-surface.md"
    ).is_file():
        reasons.append("operator surface document is missing")

    receipt_refs = tuple(
        {
            "digest": receipt.digest,
            "outcome": receipt.outcome,
            "receipt_id": receipt.receipt_id,
            "target_scope": receipt.target_scope,
            "correlation_id": receipt.correlation_id,
        }
        for receipt in list_control_receipts(receipt_dir)[:5]
    )
    ready = not reasons
    decision_profile = profile_value or "unknown"
    decision_target = target_value or "unknown"
    digest_payload = {
        "authority_ref": AUTHORITY_CONTRACT_REF,
        "profile": decision_profile,
        "ready": ready,
        "reasons": reasons,
        "receipt_refs": receipt_refs,
        "target": decision_target,
    }
    decision_digest = sha256(
        json.dumps(digest_payload, sort_keys=True).encode("utf-8"),
    ).hexdigest()
    correlation_id = build_correlation_id(
        "readiness",
        {
            "decision_digest": decision_digest,
            "profile": decision_profile,
            "ready": ready,
            "target": decision_target,
        },
    )
    metrics = operator_readiness_metrics(
        ready=ready,
        reasons=reasons,
        receipt_refs=receipt_refs,
    )
    return OperatorReadinessDecision(
        authority_refs=(AUTHORITY_CONTRACT_REF,),
        correlation_id=correlation_id,
        decision_id=f"readiness-decision:{decision_digest[:24]}",
        escalation_target=None if ready else "workspace-governance",
        mutation_boundary="fabric-local decision record only",
        metrics=metrics,
        outcome="ready" if ready else "blocked",
        profile=decision_profile,
        ready=ready,
        reasons=tuple(reasons),
        receipt_refs=receipt_refs,
        target=decision_target,
    )


def run_operator_readiness_evaluation(
    *,
    actor: str = "wgcf-local",
    ledger_path: str | Path = DEFAULT_LEDGER_PATH,
    profile: str,
    receipt_dir: str | Path = DEFAULT_RECEIPT_DIR,
    repo_root: str | Path,
    target: str,
    now: datetime | str | None = None,
) -> OperatorReadinessResult:
    """Evaluate readiness and append a fabric-local ledger event."""

    decision = evaluate_operator_readiness(
        profile=profile,
        receipt_dir=receipt_dir,
        repo_root=repo_root,
        target=target,
    )
    event = _operator_readiness_ledger_event(
        actor=actor,
        decision=decision,
        event_time=_coerce_timestamp(now),
    )
    event_path = append_ledger_event(ledger_path, event)
    return OperatorReadinessResult(
        decision=decision,
        ledger_event=event,
        ledger_path=str(event_path),
    )


def _catalog_summary_for_plan(
    catalog: CatalogManifestResult,
    plan: ValidationPlan,
) -> dict[str, Any]:
    """Return catalog metadata with plan-selected entries separated clearly.

    The generated manifest can contain every profile-admitted catalog entry.
    Operator-facing plan/check output must not call those entries "selected"
    because the planner may suppress most of them for the requested scope.
    """

    summary = catalog.to_summary_record()
    manifest_entries = list(summary["selected_entries"])
    planned_entry_ids = [
        str(check.validator_id).removeprefix("catalog:")
        for check in plan.checks
    ]
    planned_id_set = set(planned_entry_ids)
    planned_entries = [
        entry
        for entry in manifest_entries
        if entry["entry_id"] in planned_id_set
    ]
    summary["manifest_selected_entry_count"] = summary["selected_entry_count"]
    summary["manifest_selected_entries"] = manifest_entries
    summary["planned_entry_count"] = len(planned_entries)
    summary["planned_entries"] = planned_entries
    summary["selected_entry_count"] = len(planned_entries)
    summary["selected_entries"] = planned_entries
    summary["selection_note"] = (
        "selected_entries are planner-selected for this target scope; "
        "manifest_selected_entries are all profile-admitted catalog entries."
    )
    return summary


def _receipt_summary(path: Path) -> ReceiptSummary:
    record = _load_receipt_record(path)

    required = ("captured_at", "digest", "outcome", "receipt_id", "target_scope", "tier")
    missing = [field for field in required if not str(record.get(field) or "").strip()]
    if missing:
        raise ValueError(f"receipt {path} missing required fields: {', '.join(missing)}")
    artifact_refs = record.get("artifact_refs") or []
    check_results = record.get("check_results") or []
    if not isinstance(artifact_refs, list):
        raise ValueError(f"receipt {path} artifact_refs must be an array")
    if not isinstance(check_results, list):
        raise ValueError(f"receipt {path} check_results must be an array")

    return ReceiptSummary(
        artifact_count=len(artifact_refs),
        captured_at=str(record["captured_at"]),
        check_count=len(check_results),
        correlation_id=record.get("correlation_id"),
        digest=str(record["digest"]),
        metrics=record.get("metrics") if isinstance(record.get("metrics"), dict) else {},
        outcome=str(record["outcome"]),
        path=str(path),
        receipt_id=str(record["receipt_id"]),
        target_scope=str(record["target_scope"]),
        tier=str(record["tier"]),
    )


def _load_receipt_record(path: Path) -> dict[str, Any]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid receipt JSON: {path}") from exc
    if not isinstance(record, dict):
        raise ValueError(f"receipt {path} must be a JSON object")
    return record


def _resolve_receipt_ref(receipt_ref: str | Path, receipt_dir: str | Path) -> Path:
    ref = Path(receipt_ref)
    root = Path(receipt_dir).resolve()
    candidates = []
    if ref.is_absolute():
        resolved = ref.resolve()
        if resolved.is_relative_to(root):
            candidates.append(resolved)
    if not ref.is_absolute():
        candidates.extend(
            [
                root / ref,
                root / f"{_safe_file_stem(str(receipt_ref))}.json",
            ],
        )

    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_relative_to(root) and resolved.is_file():
            return resolved
    raise ValueError(f"receipt not found: {receipt_ref}")


def _target_supported(target: str) -> bool:
    return (
        target == "workspace"
        or target.startswith("repo:")
        or target.startswith("component:")
        or target.startswith("operator-surface:")
    )


def _target_authority_reasons(root: Path, target: str) -> list[str]:
    if not target or not _target_supported(target):
        return []

    known = _known_authority_targets(root)
    if target == "workspace":
        if not (root.parent / "workspace-governance").is_dir():
            return ["workspace authority repo missing: workspace-governance"]
        return []

    target_kind, target_id = target.split(":", 1)
    if target_kind == "repo" and target_id not in known["repos"]:
        return [f"unknown repo target: {target_id}"]
    if target_kind == "component" and target_id not in known["components"]:
        return [f"unknown component target: {target_id}"]
    if target_kind == "operator-surface" and target_id not in known["operator_surfaces"]:
        return [f"unknown operator surface target: {target_id}"]
    return []


def _known_authority_targets(root: Path) -> dict[str, set[str]]:
    targets = {
        "components": set(),
        "operator_surfaces": set(KNOWN_OPERATOR_SURFACE_IDS),
        "repos": {RUNTIME_REPO},
    }
    manifest_path = root / "examples/governance-manifest.example.json"
    if not manifest_path.is_file():
        return targets
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return targets
    if not isinstance(manifest, dict):
        return targets
    for repo in manifest.get("repos") or []:
        if isinstance(repo, dict) and str(repo.get("repo_id") or "").strip():
            targets["repos"].add(str(repo["repo_id"]))
    for component in manifest.get("components") or []:
        if isinstance(component, dict) and str(component.get("component_id") or "").strip():
            targets["components"].add(str(component["component_id"]))
    return targets


def _operator_readiness_ledger_event(
    *,
    actor: str,
    decision: OperatorReadinessDecision,
    event_time: datetime,
) -> LedgerEvent:
    event_time_text = event_time.isoformat().replace("+00:00", "Z")
    outcome = "success" if decision.ready else "blocked"
    receipt_refs = tuple(
        {
            key: value
            for key, value in receipt_ref.items()
            if key in {"digest", "outcome", "receipt_id"}
        }
        for receipt_ref in decision.receipt_refs
    )
    digest_payload = {
        "action": "readiness.decision.recorded",
        "actor": actor,
        "decision_id": decision.decision_id,
        "event_time": event_time_text,
        "outcome": outcome,
        "receipt_refs": receipt_refs,
        "target": decision.target,
    }
    event_digest = sha256(
        json.dumps(digest_payload, sort_keys=True).encode("utf-8"),
    ).hexdigest()
    return LedgerEvent(
        action="readiness.decision.recorded",
        actor=str(actor or "wgcf-local"),
        artifact_refs=(),
        event_id=f"ledger-event:{event_digest[:24]}",
        event_time=event_time_text,
        outcome=outcome,
        receipt_refs=receipt_refs,
        schema_version=LEDGER_SCHEMA_VERSION,
        target=decision.target,
    )


def _coerce_timestamp(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def _safe_file_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "receipt"


def _resolve_local_path(repo_root: Path, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = repo_root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(repo_root):
        raise ValueError("operator output path must stay inside the repository root")
    return resolved
