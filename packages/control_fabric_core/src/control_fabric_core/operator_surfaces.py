"""Operator-facing validation and receipt helpers.

These helpers keep CLI and API surfaces compact. Raw validator output remains
in artifact files referenced by receipts; operator responses return summaries
and paths only.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .graph_queries import load_governance_manifest_file
from .catalog_manifest import (
    CatalogManifestResult,
    build_catalog_governance_manifest,
)
from .validation_execution import (
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
            "catalog": self.catalog.to_summary_record(),
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
        record["catalog"] = self.catalog.to_summary_record()
        record["catalog_manifest_path"] = self.manifest_path
        return record


@dataclass(frozen=True)
class ReceiptSummary:
    """Operator-safe receipt metadata for list views."""

    artifact_count: int
    captured_at: str
    check_count: int
    digest: str
    outcome: str
    path: str
    receipt_id: str
    target_scope: str
    tier: str

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


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


def _receipt_summary(path: Path) -> ReceiptSummary:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid receipt JSON: {path}") from exc

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
        digest=str(record["digest"]),
        outcome=str(record["outcome"]),
        path=str(path),
        receipt_id=str(record["receipt_id"]),
        target_scope=str(record["target_scope"]),
        tier=str(record["tier"]),
    )


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
