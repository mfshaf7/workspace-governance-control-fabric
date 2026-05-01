"""Compact observability helpers for WGCF receipts and readiness decisions."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, Iterable


def build_correlation_id(namespace: str, payload: dict[str, Any]) -> str:
    """Build a stable compact correlation id without embedding raw context."""

    safe_namespace = _safe_namespace(namespace)
    digest = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()
    return f"correlation:{safe_namespace}:{digest[:24]}"


def validation_execution_metrics(
    *,
    artifact_refs: Iterable[Any],
    check_results: Iterable[Any],
    outcome: str,
) -> dict[str, Any]:
    """Summarize validation execution without reading artifact contents."""

    artifacts = list(artifact_refs)
    checks = list(check_results)
    status_counts: dict[str, int] = {}
    duration_ms = 0
    duration_present = False
    output_budget_exceeded_count = 0
    for check in checks:
        status = str(getattr(check, "status", None) or _dict_value(check, "status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        check_duration = getattr(check, "duration_ms", None)
        if check_duration is None and isinstance(check, dict):
            check_duration = check.get("duration_ms")
        if isinstance(check_duration, int):
            duration_ms += check_duration
            duration_present = True
        output_summary = getattr(check, "output_summary", None)
        if output_summary is None and isinstance(check, dict):
            output_summary = check.get("output_summary")
        if isinstance(output_summary, dict):
            budget = output_summary.get("output_budget")
            if isinstance(budget, dict) and budget.get("exceeded") is True:
                output_budget_exceeded_count += 1

    return {
        "artifact_count": len(artifacts),
        "artifact_total_bytes": sum(_int_value(artifact, "byte_count") for artifact in artifacts),
        "check_count": len(checks),
        "duration_ms": duration_ms if duration_present else None,
        "outcome": str(outcome or "unknown"),
        "output_budget_exceeded_count": output_budget_exceeded_count,
        "raw_output_embedded": False,
        "status_counts": dict(sorted(status_counts.items())),
    }


def operator_readiness_metrics(
    *,
    ready: bool,
    reasons: Iterable[str],
    receipt_refs: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize local readiness decisions for operator surfaces."""

    reason_records = list(reasons)
    receipts = list(receipt_refs)
    return {
        "blocked_reason_count": len(reason_records),
        "ready": bool(ready),
        "receipt_ref_count": len(receipts),
        "successful_receipt_ref_count": sum(1 for receipt in receipts if receipt.get("outcome") == "success"),
    }


def art_readiness_metrics(
    *,
    findings: Iterable[Any],
    graph_summary: dict[str, Any],
    mutation_allowed: bool,
    recommendations: Iterable[Any],
) -> dict[str, Any]:
    """Summarize broker-context readiness without exposing raw ART context."""

    finding_records = list(findings)
    recommendation_records = list(recommendations)
    severity_counts: dict[str, int] = {}
    for finding in finding_records:
        severity = str(getattr(finding, "severity", None) or _dict_value(finding, "severity") or "unknown")
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
    return {
        "edge_count": int(graph_summary.get("edge_count") or 0),
        "finding_count": len(finding_records),
        "mutation_allowed": bool(mutation_allowed),
        "node_count": int(graph_summary.get("node_count") or 0),
        "projection_dirty": bool(graph_summary.get("projection_dirty", False)),
        "recommendation_count": len(recommendation_records),
        "severity_counts": dict(sorted(severity_counts.items())),
    }


def receipt_metrics_snapshot(receipts: Iterable[Any]) -> dict[str, Any]:
    """Aggregate compact receipt metadata for metrics-oriented views."""

    receipt_records = [receipt.to_record() if hasattr(receipt, "to_record") else receipt for receipt in receipts]
    outcome_counts: dict[str, int] = {}
    total_artifacts = 0
    total_checks = 0
    for receipt in receipt_records:
        if not isinstance(receipt, dict):
            continue
        outcome = str(receipt.get("outcome") or "unknown")
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        total_artifacts += int(receipt.get("artifact_count") or len(receipt.get("artifact_refs") or []))
        total_checks += int(receipt.get("check_count") or len(receipt.get("check_results") or []))
    return {
        "artifact_count": total_artifacts,
        "check_count": total_checks,
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "receipt_count": len(receipt_records),
    }


def _safe_namespace(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in str(value or "wgcf").strip().lower()
    ).strip(".-_") or "wgcf"


def _dict_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return None


def _int_value(value: Any, key: str) -> int:
    raw_value = getattr(value, key, None)
    if raw_value is None and isinstance(value, dict):
        raw_value = value.get(key)
    try:
        return int(raw_value or 0)
    except (TypeError, ValueError):
        return 0
