"""Performance-budget and invocation-class controls for WGCF runtime paths.

The fabric should make governance faster, not turn every operator action into a
slow synchronous validation sweep. This module centralizes the budget contract
used by planning, graph query, validation execution, and broker-facing
integration guidance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Iterable, Sequence, TypeVar


class InvocationClass(StrEnum):
    """Runtime class used to decide how WGCF may sit in an operator path."""

    INLINE_FAST = "inline-fast"
    RECEIPT_CHECK = "receipt-check"
    HARD_GATE = "hard-gate"
    CHECKPOINT_BATCH = "checkpoint-batch"
    OFFLINE_ADVISORY = "offline-advisory"


@dataclass(frozen=True)
class PerformanceBudget:
    """Budget contract for one WGCF operation family."""

    budget_id: str
    operation: str
    invocation_class: str
    recommended_action: str
    max_duration_ms: int
    failure_mode: str
    max_graph_edges: int | None = None
    max_graph_nodes: int | None = None
    max_output_budget_bytes: int | None = None
    max_page_size: int | None = None
    max_retry_count: int | None = None
    max_selected_checks: int | None = None
    max_timeout_seconds: int | None = None
    receipt_freshness_seconds: int | None = None

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BudgetDecision:
    """Concrete budget evaluation for an operation instance."""

    budget: PerformanceBudget
    observed: dict[str, Any]
    recommended_action: str
    reasons: tuple[str, ...]
    within_budget: bool

    def to_record(self) -> dict[str, Any]:
        return {
            "budget": self.budget.to_record(),
            "invocation_class": self.budget.invocation_class,
            "observed": dict(self.observed),
            "recommended_action": self.recommended_action,
            "reasons": list(self.reasons),
            "within_budget": self.within_budget,
        }


@dataclass(frozen=True)
class ExecutionLimitDecision:
    """Effective runtime limits after applying the operation budget cap."""

    decision: BudgetDecision
    output_budget_bytes: int | None
    retry_count: int
    timeout_seconds: int

    def to_record(self) -> dict[str, Any]:
        record = self.decision.to_record()
        record["effective_limits"] = {
            "output_budget_bytes": self.output_budget_bytes,
            "retry_count": self.retry_count,
            "timeout_seconds": self.timeout_seconds,
        }
        return record


@dataclass(frozen=True)
class PaginationResult:
    """Deterministic pagination metadata for a bounded operator result."""

    effective_limit: int
    has_next_page: bool
    next_offset: int | None
    offset: int
    returned_count: int
    total_count: int

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_PROFILE = "developer"
UNKNOWN_OPERATION = "unknown"


DEFAULT_OPERATION_BUDGETS: dict[str, PerformanceBudget] = {
    "art.continuation": PerformanceBudget(
        budget_id="budget:art-continuation-inline-v1",
        operation="art.continuation",
        invocation_class=InvocationClass.INLINE_FAST.value,
        recommended_action="evaluate_cached_compact_context",
        max_duration_ms=750,
        max_graph_edges=150,
        max_graph_nodes=100,
        max_page_size=50,
        failure_mode="advisory_on_timeout",
    ),
    "art.complete": PerformanceBudget(
        budget_id="budget:art-complete-hard-gate-v1",
        operation="art.complete",
        invocation_class=InvocationClass.HARD_GATE.value,
        recommended_action="fail_closed_before_mutation",
        max_duration_ms=5000,
        failure_mode="fail_closed",
    ),
    "art.blocker": PerformanceBudget(
        budget_id="budget:art-blocker-hard-gate-v1",
        operation="art.blocker",
        invocation_class=InvocationClass.HARD_GATE.value,
        recommended_action="fail_closed_before_mutation",
        max_duration_ms=5000,
        failure_mode="fail_closed",
    ),
    "art.risk": PerformanceBudget(
        budget_id="budget:art-risk-hard-gate-v1",
        operation="art.risk",
        invocation_class=InvocationClass.HARD_GATE.value,
        recommended_action="fail_closed_before_mutation",
        max_duration_ms=5000,
        failure_mode="fail_closed",
    ),
    "artifact.output": PerformanceBudget(
        budget_id="budget:artifact-output-checkpoint-v1",
        operation="artifact.output",
        invocation_class=InvocationClass.CHECKPOINT_BATCH.value,
        recommended_action="write_artifacts_and_project_compact_refs",
        max_duration_ms=30000,
        max_output_budget_bytes=500_000,
        failure_mode="record_and_route_owner",
    ),
    "draft.submit": PerformanceBudget(
        budget_id="budget:draft-submit-receipt-check-v1",
        operation="draft.submit",
        invocation_class=InvocationClass.RECEIPT_CHECK.value,
        recommended_action="verify_payload_digest_and_fresh_receipt",
        max_duration_ms=250,
        receipt_freshness_seconds=900,
        failure_mode="require_revalidation_receipt",
    ),
    "draft.validate": PerformanceBudget(
        budget_id="budget:draft-validate-inline-v1",
        operation="draft.validate",
        invocation_class=InvocationClass.INLINE_FAST.value,
        recommended_action="validate_schema_and_emit_receipt",
        max_duration_ms=1000,
        receipt_freshness_seconds=900,
        failure_mode="return_findings_without_mutation",
    ),
    "graph.query": PerformanceBudget(
        budget_id="budget:graph-query-inline-v1",
        operation="graph.query",
        invocation_class=InvocationClass.INLINE_FAST.value,
        recommended_action="paginate_compact_graph_slice",
        max_duration_ms=500,
        max_graph_edges=100,
        max_graph_nodes=50,
        max_page_size=50,
        failure_mode="paginate_or_narrow_scope",
    ),
    "projection.sync": PerformanceBudget(
        budget_id="budget:projection-sync-checkpoint-v1",
        operation="projection.sync",
        invocation_class=InvocationClass.CHECKPOINT_BATCH.value,
        recommended_action="batch_at_projection_checkpoint",
        max_duration_ms=60_000,
        failure_mode="record_dirty_and_retry_checkpoint",
    ),
    "quality.full": PerformanceBudget(
        budget_id="budget:quality-full-checkpoint-v1",
        operation="quality.full",
        invocation_class=InvocationClass.CHECKPOINT_BATCH.value,
        recommended_action="run_batch_quality_gate",
        max_duration_ms=60_000,
        failure_mode="block_final_quality_claim",
    ),
    "validation.plan": PerformanceBudget(
        budget_id="budget:validation-plan-inline-v1",
        operation="validation.plan",
        invocation_class=InvocationClass.INLINE_FAST.value,
        recommended_action="build_compact_plan",
        max_duration_ms=1000,
        max_selected_checks=20,
        failure_mode="route_to_checkpoint_batch",
    ),
    "validation.run": PerformanceBudget(
        budget_id="budget:validation-run-hard-gate-v1",
        operation="validation.run",
        invocation_class=InvocationClass.HARD_GATE.value,
        recommended_action="run_bounded_checks_and_emit_receipt",
        max_duration_ms=120_000,
        max_output_budget_bytes=500_000,
        max_retry_count=3,
        max_timeout_seconds=120,
        failure_mode="emit_failure_receipt",
    ),
}


PROFILE_OVERRIDES: dict[str, dict[str, dict[str, int]]] = {
    "casual": {
        "graph.query": {"max_page_size": 25, "max_graph_nodes": 25, "max_graph_edges": 50},
        "validation.plan": {"max_selected_checks": 5},
    },
    "enterprise": {
        "draft.submit": {"receipt_freshness_seconds": 300},
        "validation.run": {"max_output_budget_bytes": 250_000, "max_retry_count": 1},
    },
}


T = TypeVar("T")


def resolve_performance_budget(
    operation: str,
    *,
    profile: str = DEFAULT_PROFILE,
) -> PerformanceBudget:
    """Return the budget contract for an operation/profile pair."""

    operation_key = str(operation or "").strip() or UNKNOWN_OPERATION
    base = DEFAULT_OPERATION_BUDGETS.get(operation_key)
    if base is None:
        return PerformanceBudget(
            budget_id=f"budget:unclassified-{_safe_token(operation_key)}-v1",
            operation=operation_key,
            invocation_class=InvocationClass.OFFLINE_ADVISORY.value,
            recommended_action="classify_operation_before_synchronous_gate",
            max_duration_ms=0,
            failure_mode="classification_required",
        )
    overrides = PROFILE_OVERRIDES.get(str(profile or DEFAULT_PROFILE).strip(), {}).get(operation_key, {})
    if not overrides:
        return base
    record = base.to_record()
    record.update(overrides)
    return PerformanceBudget(**record)


def evaluate_operation_budget(
    operation: str,
    *,
    observed: dict[str, Any] | None = None,
    profile: str = DEFAULT_PROFILE,
) -> BudgetDecision:
    """Evaluate observed runtime shape against the operation budget."""

    budget = resolve_performance_budget(operation, profile=profile)
    observations = dict(observed or {})
    reasons: list[str] = []
    within_budget = True
    _check_max("selected_checks", budget.max_selected_checks, observations, reasons)
    _check_max("graph_nodes", budget.max_graph_nodes, observations, reasons)
    _check_max("graph_edges", budget.max_graph_edges, observations, reasons)
    _check_max("duration_ms", budget.max_duration_ms, observations, reasons)
    _check_max("output_bytes", budget.max_output_budget_bytes, observations, reasons)
    if reasons:
        within_budget = False

    recommended_action = budget.recommended_action
    if not within_budget and budget.invocation_class == InvocationClass.INLINE_FAST.value:
        recommended_action = "route_to_checkpoint_batch_or_paginate"
    elif budget.operation == UNKNOWN_OPERATION or budget.failure_mode == "classification_required":
        recommended_action = "classify_operation_before_synchronous_gate"
        within_budget = False

    if not reasons:
        reasons.append("within configured WGCF performance budget")
    return BudgetDecision(
        budget=budget,
        observed=observations,
        recommended_action=recommended_action,
        reasons=tuple(reasons),
        within_budget=within_budget,
    )


def evaluate_validation_plan_budget(
    *,
    profile: str = DEFAULT_PROFILE,
    selected_check_count: int,
    tier: str,
) -> BudgetDecision:
    """Evaluate whether a validation plan is cheap enough for inline use."""

    observed = {
        "selected_checks": selected_check_count,
        "tier": str(tier),
    }
    return evaluate_operation_budget("validation.plan", observed=observed, profile=profile)


def coerce_execution_limits(
    execution_policy: dict[str, Any],
    *,
    default_timeout_seconds: int,
    profile: str = DEFAULT_PROFILE,
) -> ExecutionLimitDecision:
    """Apply the validation-run budget as a cap over manifest execution policy."""

    budget = resolve_performance_budget("validation.run", profile=profile)
    timeout_seconds = _int_or_default(
        execution_policy.get("timeout_seconds"),
        default=default_timeout_seconds,
        minimum=1,
    )
    retry_count = _int_or_default(execution_policy.get("retry_count"), default=0, minimum=0)
    output_budget_bytes = _optional_int(execution_policy.get("output_budget_bytes"), minimum=0)

    observed = {
        "requested_output_budget_bytes": output_budget_bytes,
        "requested_retry_count": retry_count,
        "requested_timeout_seconds": timeout_seconds,
    }
    reasons: list[str] = []
    if budget.max_timeout_seconds is not None and timeout_seconds > budget.max_timeout_seconds:
        reasons.append(
            f"timeout_seconds capped from {timeout_seconds} to {budget.max_timeout_seconds}",
        )
        timeout_seconds = budget.max_timeout_seconds
    if budget.max_retry_count is not None and retry_count > budget.max_retry_count:
        reasons.append(f"retry_count capped from {retry_count} to {budget.max_retry_count}")
        retry_count = budget.max_retry_count
    if budget.max_output_budget_bytes is not None:
        if output_budget_bytes is None:
            output_budget_bytes = budget.max_output_budget_bytes
            reasons.append(f"output_budget_bytes defaulted to {budget.max_output_budget_bytes}")
        elif output_budget_bytes > budget.max_output_budget_bytes:
            reasons.append(
                "output_budget_bytes capped from "
                f"{output_budget_bytes} to {budget.max_output_budget_bytes}",
            )
            output_budget_bytes = budget.max_output_budget_bytes

    if not reasons:
        reasons.append("execution policy is within validation-run budget")
    decision = BudgetDecision(
        budget=budget,
        observed=observed,
        recommended_action=budget.recommended_action,
        reasons=tuple(reasons),
        within_budget=not any("capped" in reason for reason in reasons),
    )
    return ExecutionLimitDecision(
        decision=decision,
        output_budget_bytes=output_budget_bytes,
        retry_count=retry_count,
        timeout_seconds=timeout_seconds,
    )


def paginate_items(
    items: Sequence[T],
    *,
    limit: int | None,
    offset: int = 0,
    operation: str = "graph.query",
    profile: str = DEFAULT_PROFILE,
) -> tuple[tuple[T, ...], PaginationResult]:
    """Return a budget-bounded page for an operator-visible sequence."""

    budget = resolve_performance_budget(operation, profile=profile)
    max_page_size = budget.max_page_size or len(items) or 1
    effective_limit = _int_or_default(limit, default=max_page_size, minimum=1)
    effective_limit = min(effective_limit, max_page_size)
    effective_offset = max(_int_or_default(offset, default=0, minimum=0), 0)
    page = tuple(items[effective_offset:effective_offset + effective_limit])
    next_offset = effective_offset + effective_limit
    has_next_page = next_offset < len(items)
    return page, PaginationResult(
        effective_limit=effective_limit,
        has_next_page=has_next_page,
        next_offset=next_offset if has_next_page else None,
        offset=effective_offset,
        returned_count=len(page),
        total_count=len(items),
    )


def operation_budget_records(
    operations: Iterable[str] | None = None,
    *,
    profile: str = DEFAULT_PROFILE,
) -> tuple[dict[str, Any], ...]:
    """Return budget records for operator/API inspection."""

    operation_names = tuple(operations or sorted(DEFAULT_OPERATION_BUDGETS))
    return tuple(
        resolve_performance_budget(operation, profile=profile).to_record()
        for operation in operation_names
    )


def _check_max(
    label: str,
    limit: int | None,
    observed: dict[str, Any],
    reasons: list[str],
) -> None:
    if limit is None:
        return
    value = observed.get(label)
    if value is None:
        return
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return
    if parsed > limit:
        reasons.append(f"{label} {parsed} exceeds budget {limit}")


def _int_or_default(value: Any, *, default: int, minimum: int) -> int:
    parsed = _optional_int(value, minimum=minimum)
    return default if parsed is None else parsed


def _optional_int(value: Any, *, minimum: int) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= minimum else None


def _safe_token(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value.lower()).strip("-") or "operation"
