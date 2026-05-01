"""ART runtime-context ingestion and readiness helpers.

The control fabric consumes broker-owned ART context as read-only input. It
does not mutate OpenProject or replace operator-orchestration-service as the
ART write authority.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Iterable


ART_READINESS_SCHEMA_VERSION = 1
DEFAULT_COMPLETION_HEADINGS = (
    "Completion Summary",
    "Changed Surfaces",
    "Test Result Evidence",
    "Validation Evidence",
)
FEATURE_NARRATIVE_HEADINGS = (
    "What This Enables",
    "Benefit Hypothesis",
    "Scope Boundaries",
    "Evidence Expectation",
    "Execution Context",
    "Operator work notes",
)
ERROR_SEVERITIES = {"error", "blocker"}


@dataclass(frozen=True)
class ArtRuntimeNode:
    """Compact node derived from broker-owned ART context."""

    node_id: str
    node_type: str
    owner_repo: str | None
    record_ref: str | None
    status: str | None
    properties: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArtRuntimeEdge:
    """Compact relationship derived from broker-owned ART context."""

    edge_id: str
    source_node_id: str
    target_node_id: str
    edge_type: str
    properties: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArtRuntimeGraph:
    """Operator-safe ART graph projection for readiness checks."""

    captured_at: str
    graph_id: str
    nodes: tuple[ArtRuntimeNode, ...]
    edges: tuple[ArtRuntimeEdge, ...]
    source_surfaces: tuple[str, ...]
    summary: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at,
            "edges": [edge.to_record() for edge in self.edges],
            "graph_id": self.graph_id,
            "nodes": [node.to_record() for node in self.nodes],
            "source_surfaces": list(self.source_surfaces),
            "summary": self.summary,
        }


@dataclass(frozen=True)
class ArtReadinessFinding:
    """One pre-mutation readiness finding."""

    code: str
    message: str
    owner_repo: str | None
    recommended_route: str
    severity: str
    target: str

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArtReadinessRecommendation:
    """Deterministic next action for OOS or the operator."""

    action: str
    decision_path: str
    owner_repo: str | None
    reason: str
    route: str
    target: str

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArtReadinessReceipt:
    """Compact readiness receipt for one planned ART operation."""

    captured_at: str
    findings: tuple[ArtReadinessFinding, ...]
    graph_summary: dict[str, Any]
    mutation_allowed: bool
    operation: str
    outcome: str
    projection_sync_recommended: bool
    raw_context_embedded: bool
    receipt_id: str
    recommendations: tuple[ArtReadinessRecommendation, ...]
    schema_version: int
    source_surfaces: tuple[str, ...]
    target_item_id: str | None

    def to_record(self) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at,
            "findings": [finding.to_record() for finding in self.findings],
            "graph_summary": self.graph_summary,
            "mutation_allowed": self.mutation_allowed,
            "operation": self.operation,
            "outcome": self.outcome,
            "projection_sync_recommended": self.projection_sync_recommended,
            "raw_context_embedded": self.raw_context_embedded,
            "receipt_id": self.receipt_id,
            "recommendations": [
                recommendation.to_record()
                for recommendation in self.recommendations
            ],
            "schema_version": self.schema_version,
            "source_surfaces": list(self.source_surfaces),
            "target_item_id": self.target_item_id,
        }


@dataclass(frozen=True)
class ArtEvidencePacket:
    """Broker-safe completion and Review Packet projection from receipts."""

    completion_payload: dict[str, str]
    denied_or_suppressed: tuple[str, ...]
    included_evidence: tuple[str, ...]
    item_evidence_refs: tuple[dict[str, str], ...]
    packet_id: str
    packet_time: str
    raw_artifacts_embedded: bool
    receipt_refs: tuple[dict[str, str], ...]
    review_packet: dict[str, Any]
    schema_version: int
    target_item_ids: tuple[str, ...]

    def to_record(self) -> dict[str, Any]:
        return {
            "completion_payload": self.completion_payload,
            "denied_or_suppressed": list(self.denied_or_suppressed),
            "included_evidence": list(self.included_evidence),
            "item_evidence_refs": list(self.item_evidence_refs),
            "packet_id": self.packet_id,
            "packet_time": self.packet_time,
            "raw_artifacts_embedded": self.raw_artifacts_embedded,
            "receipt_refs": list(self.receipt_refs),
            "review_packet": self.review_packet,
            "schema_version": self.schema_version,
            "target_item_ids": list(self.target_item_ids),
        }


def build_art_runtime_graph(
    broker_context: dict[str, Any],
    *,
    now: datetime | str | None = None,
) -> ArtRuntimeGraph:
    """Project broker ART context into compact graph nodes and edges."""

    context = _context_root(broker_context)
    captured_at = _coerce_timestamp(now).isoformat().replace("+00:00", "Z")
    nodes: dict[str, ArtRuntimeNode] = {}
    edges: dict[str, ArtRuntimeEdge] = {}
    source_surfaces = _source_surfaces(broker_context)

    for raw_item in _iter_art_items(context):
        item = _normalize_item(raw_item)
        if item is None:
            continue
        node_id = _item_node_id(item["id"])
        node = ArtRuntimeNode(
            node_id=node_id,
            node_type=str(item.get("type") or "work-item"),
            owner_repo=item.get("owner_repo"),
            record_ref=item.get("record_ref"),
            status=item.get("status"),
            properties=item,
        )
        nodes[node_id] = node

    delivery_epic = _normalize_item(context.get("delivery_epic") or broker_context.get("delivery_epic") or {})
    if delivery_epic:
        epic_node_id = _item_node_id(delivery_epic["id"])
        for node in tuple(nodes.values()):
            parent_id = node.properties.get("parent_id")
            if parent_id:
                _add_edge(edges, _item_node_id(parent_id), node.node_id, "parent-of")
            elif node.node_id != epic_node_id and node.node_type != "projection-state":
                _add_edge(edges, epic_node_id, node.node_id, "delivery-contains")

    for relation in _relation_records(context):
        source_id = relation.get("source_id")
        target_id = relation.get("target_id")
        relation_type = relation.get("relation_type") or "related-to"
        if source_id and target_id:
            _add_edge(
                edges,
                _item_node_id(source_id),
                _item_node_id(target_id),
                str(relation_type),
                {"source": "broker-context"},
            )

    projection_state = _projection_state(broker_context)
    if projection_state:
        projection_node = ArtRuntimeNode(
            node_id="art-projection-state",
            node_type="projection-state",
            owner_repo="operator-orchestration-service",
            record_ref=None,
            status="dirty" if projection_state.get("dirty") else "clean",
            properties={
                "affected_delivery_ids": projection_state.get("affected_delivery_ids") or [],
                "affected_work_item_ids": projection_state.get("affected_work_item_ids") or [],
                "dirty": bool(projection_state.get("dirty")),
                "next_action": projection_state.get("next_action"),
                "updated_at": projection_state.get("updated_at"),
            },
        )
        nodes[projection_node.node_id] = projection_node

    summary = _graph_summary(nodes.values(), broker_context)
    digest_payload = {
        "captured_at": captured_at,
        "edges": [edge.to_record() for edge in sorted(edges.values(), key=lambda item: item.edge_id)],
        "nodes": [node.to_record() for node in sorted(nodes.values(), key=lambda item: item.node_id)],
        "source_surfaces": source_surfaces,
        "summary": summary,
    }
    graph_digest = _digest_json(digest_payload).removeprefix("sha256:")
    return ArtRuntimeGraph(
        captured_at=captured_at,
        edges=tuple(edges[key] for key in sorted(edges)),
        graph_id=f"art-runtime-graph:{graph_digest[:24]}",
        nodes=tuple(nodes[key] for key in sorted(nodes)),
        source_surfaces=source_surfaces,
        summary=summary,
    )


def evaluate_art_readiness(
    broker_context: dict[str, Any],
    *,
    operation: str = "complete",
    target_item_id: int | str | None = None,
    now: datetime | str | None = None,
) -> ArtReadinessReceipt:
    """Evaluate whether OOS should proceed with an ART mutation."""

    context = _context_root(broker_context)
    graph = build_art_runtime_graph(broker_context, now=now)
    captured_at = graph.captured_at
    target_item = _target_item(context, target_item_id)
    target_id = _string_id(target_item.get("id")) if target_item else _string_id(target_item_id)
    operation_name = operation.strip() or "complete"
    findings = list(_readiness_findings(broker_context, context, target_item, operation_name))
    has_errors = any(finding.severity in ERROR_SEVERITIES for finding in findings)
    mutation_allowed = not has_errors
    outcome = "blocked" if has_errors else "review_required" if findings else "ready"
    recommendations = tuple(_recommendations(findings, target_id, mutation_allowed))
    projection_sync_recommended = any(
        recommendation.action == "projection_sync"
        for recommendation in recommendations
    )
    digest_payload = {
        "captured_at": captured_at,
        "findings": [finding.to_record() for finding in findings],
        "graph_id": graph.graph_id,
        "mutation_allowed": mutation_allowed,
        "operation": operation_name,
        "target_item_id": target_id,
    }
    receipt_digest = _digest_json(digest_payload).removeprefix("sha256:")
    return ArtReadinessReceipt(
        captured_at=captured_at,
        findings=tuple(findings),
        graph_summary=graph.summary,
        mutation_allowed=mutation_allowed,
        operation=operation_name,
        outcome=outcome,
        projection_sync_recommended=projection_sync_recommended,
        raw_context_embedded=False,
        receipt_id=f"art-readiness-receipt:{receipt_digest[:24]}",
        recommendations=recommendations,
        schema_version=ART_READINESS_SCHEMA_VERSION,
        source_surfaces=graph.source_surfaces,
        target_item_id=target_id,
    )


def project_receipts_to_art_evidence_packet(
    receipts: Iterable[dict[str, Any]],
    *,
    changed_surfaces: Iterable[str],
    completion_summary: str,
    item_ids: Iterable[int | str],
    residual_follow_up: Iterable[str] = (),
    now: datetime | str | None = None,
) -> ArtEvidencePacket:
    """Generate compact ART completion and Review Packet evidence."""

    receipt_records = [_receipt_record(receipt) for receipt in receipts]
    if not receipt_records:
        raise ValueError("at least one receipt is required")
    target_item_ids = tuple(_string_id(item_id) for item_id in item_ids if _string_id(item_id))
    if not target_item_ids:
        raise ValueError("at least one item id is required")
    packet_time = _coerce_timestamp(now).isoformat().replace("+00:00", "Z")
    receipt_refs = tuple(_receipt_ref(receipt) for receipt in receipt_records)
    item_evidence_refs = tuple(
        {
            "evidence_type": "control_receipt",
            "item_id": item_id,
            "receipt_digest": ref["digest"],
            "receipt_id": ref["receipt_id"],
        }
        for item_id in target_item_ids
        for ref in receipt_refs
    )
    changed_surface_lines = tuple(_clean_lines(changed_surfaces))
    residual_lines = tuple(_clean_lines(residual_follow_up))
    test_lines = tuple(_prefixed_check_lines(receipt_records))
    validation_lines = tuple(_prefixed_validation_lines(receipt_records))
    completion_payload = {
        "changed_surfaces": _markdown_list(changed_surface_lines),
        "completion_note": (
            f"Evidence projected from {len(receipt_refs)} WGCF receipt(s). "
            "Raw artifacts remain receipt-linked and are not embedded in ART."
        ),
        "completion_summary": completion_summary.strip(),
        "test_result_evidence": _markdown_list(test_lines),
        "validation_evidence": _markdown_list(validation_lines),
    }
    if residual_lines:
        completion_payload["residual_follow_up"] = _markdown_list(
            f"CHECK: {line}" if not _has_evidence_prefix(line) else line
            for line in residual_lines
        )
    review_packet = {
        "changed_surface_explanations": list(changed_surface_lines),
        "item_evidence_refs": list(item_evidence_refs),
        "rollback_boundary": "Rollback by reverting the source change; WGCF evidence remains receipt-linked.",
        "test_evidence": list(test_lines),
        "validation_evidence": list(validation_lines),
    }
    included_evidence = tuple(
        f"receipt `{ref['receipt_id']}` outcome `{ref['outcome']}` for `{ref['target_scope']}`"
        for ref in receipt_refs
    )
    digest_payload = {
        "item_evidence_refs": item_evidence_refs,
        "packet_time": packet_time,
        "receipt_refs": receipt_refs,
        "target_item_ids": target_item_ids,
    }
    packet_digest = _digest_json(digest_payload).removeprefix("sha256:")
    return ArtEvidencePacket(
        completion_payload=completion_payload,
        denied_or_suppressed=(
            "raw validator stdout/stderr omitted from ART completion payload",
            "full artifacts remain referenced by receipt digest and artifact refs",
        ),
        included_evidence=included_evidence,
        item_evidence_refs=item_evidence_refs,
        packet_id=f"art-evidence-packet:{packet_digest[:24]}",
        packet_time=packet_time,
        raw_artifacts_embedded=False,
        receipt_refs=receipt_refs,
        review_packet=review_packet,
        schema_version=ART_READINESS_SCHEMA_VERSION,
        target_item_ids=target_item_ids,
    )


def _context_root(broker_context: dict[str, Any]) -> dict[str, Any]:
    return dict(broker_context.get("continuation_context") or broker_context)


def _source_surfaces(broker_context: dict[str, Any]) -> tuple[str, ...]:
    surfaces = []
    for key in (
        "continuation_context",
        "execution_summary",
        "planning_summary",
        "quality_pack",
        "roadmap",
        "pm2_projection",
        "projection_state",
    ):
        if key in broker_context:
            surfaces.append(key)
    if not surfaces:
        surfaces.append("broker_context")
    return tuple(surfaces)


def _iter_art_items(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if _looks_like_art_item(value):
            yield value
        for nested in value.values():
            yield from _iter_art_items(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_art_items(nested)


def _looks_like_art_item(value: dict[str, Any]) -> bool:
    item_id = value.get("id")
    return item_id is not None and any(
        key in value
        for key in (
            "record_ref",
            "recordRef",
            "subject",
            "target_pi",
            "targetPi",
            "type",
            "status",
        )
    )


def _normalize_item(value: dict[str, Any]) -> dict[str, Any] | None:
    if not value or value.get("id") is None:
        return None
    item_id = _string_id(value.get("id"))
    if not item_id:
        return None
    return {
        "architecture_anchor_ref": _optional_string(value.get("architecture_anchor_ref")),
        "assignee_login": _optional_string(value.get("assignee_login") or value.get("assigneeLogin")),
        "blocked": bool(value.get("blocked")),
        "delivery_team": _optional_string(value.get("delivery_team") or value.get("deliveryTeam")),
        "dependency_blocked": bool(value.get("dependency_blocked") or value.get("dependencyBlocked")),
        "description_headings": tuple(value.get("descriptionHeadings") or value.get("description_headings") or ()),
        "description_present": bool(value.get("descriptionPresent") or value.get("description_present")),
        "execution_classification": _optional_string(
            value.get("execution_classification") or value.get("executionClassification"),
        ),
        "id": item_id,
        "iteration": _optional_string(value.get("iteration")),
        "owner_repo": _optional_string(value.get("owner_repo") or value.get("ownerRepo")),
        "parent_id": _string_id(value.get("parent_id") or value.get("parentId")) or None,
        "record_ref": _optional_string(value.get("record_ref") or value.get("recordRef")),
        "required_upstream_ref": _optional_string(value.get("required_upstream_ref")),
        "responsible_login": _optional_string(value.get("responsible_login") or value.get("responsibleLogin")),
        "status": _optional_string(value.get("status")),
        "subject": _optional_string(value.get("subject")),
        "target_pi": _optional_string(value.get("target_pi") or value.get("targetPi")),
        "type": _optional_string(value.get("type")),
    }


def _relation_records(context: dict[str, Any]) -> Iterable[dict[str, Any]]:
    dependency_context = context.get("dependency_context") or {}
    for key, relation_type in (
        ("depends_on", "depends-on"),
        ("required_by", "required-by"),
        ("unresolved_dependencies", "blocked-by-dependency"),
    ):
        records = dependency_context.get(key) or []
        if isinstance(records, list):
            for record in records:
                if isinstance(record, dict):
                    yield {
                        "source_id": record.get("source_id") or record.get("from_id") or record.get("id"),
                        "target_id": record.get("target_id") or record.get("to_id") or record.get("targetId"),
                        "relation_type": record.get("relation_type") or relation_type,
                    }


def _projection_state(broker_context: dict[str, Any]) -> dict[str, Any] | None:
    state = broker_context.get("projection_state")
    if isinstance(state, dict):
        return state
    pm2_projection = broker_context.get("pm2_projection")
    if isinstance(pm2_projection, dict) and (
        "dirty" in pm2_projection
        or str(pm2_projection.get("status") or "").lower() == "dirty"
    ):
        return {
            "affected_delivery_ids": pm2_projection.get("affected_delivery_ids") or [],
            "affected_work_item_ids": pm2_projection.get("affected_work_item_ids") or [],
            "dirty": bool(
                pm2_projection.get("dirty")
                or str(pm2_projection.get("status") or "").lower() == "dirty",
            ),
            "next_action": pm2_projection.get("next_action"),
            "updated_at": pm2_projection.get("updated_at"),
        }
    if broker_context.get("workflow_id") == "delivery-art-projection-state":
        return broker_context
    return None


def _quality_pack_unhealthy(broker_context: dict[str, Any]) -> bool:
    quality_pack = broker_context.get("quality_pack")
    if not isinstance(quality_pack, (dict, list)):
        return False
    unhealthy_values = {
        "error",
        "failed",
        "failure",
        "red",
        "unhealthy",
    }
    return any(value in unhealthy_values for value in _quality_state_values(quality_pack))


def _quality_state_values(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in {"conclusion", "health", "outcome", "status"} and isinstance(nested, str):
                yield nested.strip().lower()
            yield from _quality_state_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _quality_state_values(nested)


def _graph_summary(nodes: Iterable[ArtRuntimeNode], broker_context: dict[str, Any]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_owner: dict[str, int] = {}
    node_list = list(nodes)
    for node in node_list:
        status = node.status or "unknown"
        node_type = node.node_type or "unknown"
        owner = node.owner_repo or "unknown"
        by_status[status] = by_status.get(status, 0) + 1
        by_type[node_type] = by_type.get(node_type, 0) + 1
        by_owner[owner] = by_owner.get(owner, 0) + 1
    context = _context_root(broker_context)
    summary = context.get("summary") or broker_context.get("planning_summary", {}).get("summary") or {}
    projection = _projection_state(broker_context) or {}
    return {
        "by_owner_repo": dict(sorted(by_owner.items())),
        "by_status": dict(sorted(by_status.items())),
        "by_type": dict(sorted(by_type.items())),
        "node_count": len(node_list),
        "open_child_count": summary.get("open_child_count"),
        "projection_dirty": bool(projection.get("dirty")),
        "ready_without_contract_count": summary.get("ready_without_contract_count", 0),
    }


def _target_item(context: dict[str, Any], target_item_id: int | str | None) -> dict[str, Any] | None:
    normalized_target = _string_id(target_item_id)
    if normalized_target:
        for item in _iter_art_items(context):
            normalized = _normalize_item(item)
            if normalized and normalized["id"] == normalized_target:
                return normalized
    return _normalize_item(context.get("target_item") or {})


def _readiness_findings(
    broker_context: dict[str, Any],
    context: dict[str, Any],
    target_item: dict[str, Any] | None,
    operation: str,
) -> Iterable[ArtReadinessFinding]:
    target = f"work-item:{target_item['id']}" if target_item else "work-item:unknown"
    owner_repo = target_item.get("owner_repo") if target_item else None
    if target_item is None:
        yield _finding(
            "missing-target-item",
            "error",
            target,
            owner_repo,
            "Target work item is not present in broker continuation context.",
            "broker-continuation-context",
        )
        return

    if target_item.get("blocked") or target_item.get("dependency_blocked"):
        yield _finding(
            "target-blocked",
            "error",
            target,
            owner_repo,
            "Target work item is blocked or dependency-blocked; mutation must not proceed.",
            "work-item.blocker",
        )
    for field, code in (
        ("owner_repo", "missing-owner-repo"),
        ("target_pi", "missing-target-pi"),
    ):
        if not target_item.get(field):
            yield _finding(
                code,
                "error",
                target,
                owner_repo,
                f"Target work item is missing required `{field}` metadata.",
                "work-item.update",
            )
    if target_item.get("type") not in {"Epic", "Risk", "Milestone"}:
        for field, code in (
            ("delivery_team", "missing-delivery-team"),
            ("iteration", "missing-iteration"),
        ):
            if not target_item.get(field):
                yield _finding(
                    code,
                    "error",
                    target,
                    owner_repo,
                    f"Target work item is missing required `{field}` metadata.",
                    "work-item.update",
                )

    if operation in {"complete", "stale-open-close"}:
        missing = _missing_feature_headings(target_item)
        if missing:
            yield _finding(
                "weak-feature-narrative",
                "error",
                target,
                owner_repo,
                "Feature narrative is missing required heading(s): " + ", ".join(missing),
                "work-item.update",
            )

    summary = context.get("summary") or {}
    if (
        target_item.get("type") == "Feature"
        and target_item.get("status") in {"ready", "in-progress"}
        and summary.get("open_child_count") == 0
        and summary.get("completed_related_count", 0) > 0
    ):
        yield _finding(
            "stale-open-parent",
            "warning",
            target,
            owner_repo,
            "Feature has no open children and completed related scope; treat as stale-open closeout candidate.",
            "work-item.stale-open-close",
        )

    projection = _projection_state(broker_context)
    if projection and projection.get("dirty"):
        yield _finding(
            "projection-sync-required",
            "error",
            "delivery-art-projection",
            "operator-orchestration-service",
            "Roadmap or PM2 projection state is dirty; sync projection before final readiness or roadmap claims.",
            "projection.sync",
        )

    if _quality_pack_unhealthy(broker_context):
        yield _finding(
            "quality-pack-unhealthy",
            "error",
            "delivery:quality-pack",
            "operator-orchestration-service",
            "Broker quality context contains an unhealthy, failed, or error state.",
            "quality.check",
        )

    planning_summary = broker_context.get("planning_summary", {}).get("summary") or {}
    ready_without_contract = int(planning_summary.get("ready_without_contract_count") or 0)
    if ready_without_contract > 0:
        yield _finding(
            "ready-without-contract",
            "warning",
            "delivery:planning",
            "operator-orchestration-service",
            f"{ready_without_contract} ready item(s) lack continuation contract confidence in planning summary.",
            "item.continuation",
        )

    for item in _iter_art_items(context):
        normalized = _normalize_item(item)
        if not normalized or normalized.get("type") != "Milestone":
            continue
        if normalized.get("parent_id") != _string_id((context.get("delivery_epic") or {}).get("id")):
            yield _finding(
                "milestone-parent-drift",
                "warning",
                f"work-item:{normalized['id']}",
                normalized.get("owner_repo"),
                "Milestone should remain an Epic-level checkpoint, not a leaf substitute.",
                "work-item.move",
            )


def _recommendations(
    findings: Iterable[ArtReadinessFinding],
    target_item_id: str | None,
    mutation_allowed: bool,
) -> Iterable[ArtReadinessRecommendation]:
    emitted: set[str] = set()
    for finding in findings:
        if finding.code == "projection-sync-required":
            emitted.add("projection_sync")
            yield _recommendation(
                "projection_sync",
                "remove",
                finding,
                "Run broker-owned projection sync before roadmap or readiness closeout.",
            )
        elif finding.code in {
            "missing-delivery-team",
            "missing-iteration",
            "missing-owner-repo",
            "missing-target-pi",
            "weak-feature-narrative",
        }:
            emitted.add("repair_art_metadata")
            yield _recommendation(
                "repair_art_metadata",
                "remove",
                finding,
                "Repair metadata or narrative through an OOS draft before mutation.",
            )
        elif finding.code == "stale-open-parent":
            emitted.add("stale_open_close")
            yield _recommendation(
                "stale_open_close",
                "remove",
                finding,
                "Use stale-open closeout when completed child scope already satisfies the parent.",
            )
        elif finding.code == "target-blocked":
            emitted.add("respect_blocker")
            yield _recommendation(
                "respect_blocker",
                "defer",
                finding,
                "Keep the blocker as the controlling path until resolved.",
            )
        elif finding.code == "quality-pack-unhealthy":
            emitted.add("record_blocker")
            yield _recommendation(
                "record_blocker",
                "defer",
                finding,
                "Record an immediate blocker before any ART mutation that depends on unhealthy quality evidence.",
            )
            emitted.add("route_defect")
            yield _recommendation(
                "route_defect",
                "remove",
                finding,
                "Open or update a Defect for the failing quality path instead of patching around it.",
            )
        elif finding.code in {"milestone-parent-drift", "ready-without-contract"}:
            emitted.add("route_risk")
            yield _recommendation(
                "route_risk",
                "accept-risk",
                finding,
                "Route broad planning drift as risk when it affects more than one mutation boundary.",
            )
        elif finding.severity in ERROR_SEVERITIES:
            emitted.add("record_blocker")
            yield _recommendation(
                "record_blocker",
                "defer",
                finding,
                "Record an immediate blocker if the planned ART mutation cannot safely proceed.",
            )
        else:
            emitted.add("operator_review")
            yield _recommendation(
                "operator_review",
                "accept-risk",
                finding,
                "Review warning before proceeding; do not bypass OOS mutation authority.",
            )
    if mutation_allowed and "proceed_via_oos_broker" not in emitted:
        yield ArtReadinessRecommendation(
            action="proceed_via_oos_broker",
            decision_path="remove",
            owner_repo="operator-orchestration-service",
            reason="No blocking WGCF ART readiness findings detected.",
            route="work-item.complete",
            target=f"work-item:{target_item_id}" if target_item_id else "work-item:unknown",
        )


def _missing_feature_headings(item: dict[str, Any]) -> tuple[str, ...]:
    if item.get("type") != "Feature":
        return ()
    headings = set(str(heading).strip() for heading in item.get("description_headings") or ())
    if not headings and not item.get("description_present"):
        return FEATURE_NARRATIVE_HEADINGS
    return tuple(heading for heading in FEATURE_NARRATIVE_HEADINGS if heading not in headings)


def _finding(
    code: str,
    severity: str,
    target: str,
    owner_repo: str | None,
    message: str,
    recommended_route: str,
) -> ArtReadinessFinding:
    return ArtReadinessFinding(
        code=code,
        message=message,
        owner_repo=owner_repo,
        recommended_route=recommended_route,
        severity=severity,
        target=target,
    )


def _recommendation(
    action: str,
    decision_path: str,
    finding: ArtReadinessFinding,
    reason: str,
) -> ArtReadinessRecommendation:
    return ArtReadinessRecommendation(
        action=action,
        decision_path=decision_path,
        owner_repo=finding.owner_repo,
        reason=reason,
        route=finding.recommended_route,
        target=finding.target,
    )


def _receipt_record(receipt: dict[str, Any]) -> dict[str, Any]:
    required = ("captured_at", "digest", "outcome", "receipt_id", "target_scope")
    missing = [key for key in required if not receipt.get(key)]
    if missing:
        raise ValueError(f"receipt missing required fields: {', '.join(missing)}")
    return dict(receipt)


def _receipt_ref(receipt: dict[str, Any]) -> dict[str, str]:
    return {
        "captured_at": str(receipt["captured_at"]),
        "digest": str(receipt["digest"]),
        "outcome": str(receipt["outcome"]),
        "receipt_id": str(receipt["receipt_id"]),
        "target_scope": str(receipt["target_scope"]),
    }


def _prefixed_validation_lines(receipts: Iterable[dict[str, Any]]) -> list[str]:
    lines = []
    for receipt in receipts:
        prefix = "PASS" if receipt.get("outcome") == "success" else "FAIL"
        lines.append(
            f"{prefix}: Control receipt `{receipt['receipt_id']}` outcome "
            f"`{receipt['outcome']}` for `{receipt['target_scope']}` with digest `{receipt['digest']}`.",
        )
        if receipt.get("artifact_refs"):
            lines.append(f"CHECK: Receipt `{receipt['receipt_id']}` retains artifact refs by digest only.")
    return lines


def _prefixed_check_lines(receipts: Iterable[dict[str, Any]]) -> list[str]:
    lines = []
    for receipt in receipts:
        for result in receipt.get("check_results") or []:
            if not isinstance(result, dict):
                continue
            status = str(result.get("status") or "unknown")
            prefix = "PASS" if status in {"success", "skipped_fresh_receipt"} else "FAIL"
            validator_id = str(result.get("validator_id") or "unknown-validator")
            check_id = str(result.get("check_id") or "unknown-check")
            exit_code = result.get("exit_code")
            suffix = f" exit code `{exit_code}`." if exit_code is not None else "."
            lines.append(
                f"{prefix}: Validator `{validator_id}` check `{check_id}` status `{status}`{suffix}",
            )
    return lines or ["NOT APPLICABLE: No individual check results were present in the receipt."]


def _markdown_list(values: Iterable[str]) -> str:
    return "\n".join(f"- {value}" for value in _clean_lines(values))


def _clean_lines(values: Iterable[str]) -> list[str]:
    return [str(value).strip() for value in values if str(value).strip()]


def _has_evidence_prefix(value: str) -> bool:
    return value.startswith(("PASS:", "FAIL:", "CHECK:", "NOT APPLICABLE:", "Attached artifact:"))


def _add_edge(
    edges: dict[str, ArtRuntimeEdge],
    source_node_id: str,
    target_node_id: str,
    edge_type: str,
    properties: dict[str, Any] | None = None,
) -> None:
    if source_node_id == target_node_id:
        return
    edge_id = "art-edge:" + sha256(
        f"{source_node_id}|{edge_type}|{target_node_id}".encode("utf-8"),
    ).hexdigest()[:24]
    edges[edge_id] = ArtRuntimeEdge(
        edge_id=edge_id,
        edge_type=edge_type,
        properties=properties or {},
        source_node_id=source_node_id,
        target_node_id=target_node_id,
    )


def _item_node_id(item_id: int | str) -> str:
    return f"art-item:{_string_id(item_id)}"


def _string_id(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


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


def _digest_json(value: dict[str, Any]) -> str:
    return "sha256:" + sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    ).hexdigest()
