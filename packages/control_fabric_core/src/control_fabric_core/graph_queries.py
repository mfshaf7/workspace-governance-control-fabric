"""Read-only query helpers for manifest-derived governance graphs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .graph_ingestion import ManifestGraph, ManifestGraphEdge, ManifestGraphNode, build_manifest_graph
from .performance_budgets import evaluate_operation_budget, paginate_items


@dataclass(frozen=True)
class ManifestGraphQueryResult:
    """Compact graph slice returned for one operator query scope."""

    budget_decision: dict[str, Any]
    edge_pagination: dict[str, Any]
    manifest_id: str
    nodes: tuple[ManifestGraphNode, ...]
    edges: tuple[ManifestGraphEdge, ...]
    node_pagination: dict[str, Any]
    scope: str

    def to_record(self) -> dict[str, Any]:
        return {
            "budget_decision": self.budget_decision,
            "edges": [edge.to_record() for edge in self.edges],
            "edge_pagination": self.edge_pagination,
            "manifest_id": self.manifest_id,
            "nodes": [node.to_record() for node in self.nodes],
            "node_pagination": self.node_pagination,
            "scope": self.scope,
            "summary": {
                "edge_count": len(self.edges),
                "edge_total_count": self.edge_pagination["total_count"],
                "node_count": len(self.nodes),
                "node_total_count": self.node_pagination["total_count"],
            },
        }


def load_governance_manifest_file(manifest_path: str | Path) -> dict[str, Any]:
    """Load a governance manifest JSON file without mutating any authority store."""

    path = Path(manifest_path)
    return json.loads(path.read_text(encoding="utf-8"))


def build_graph_from_manifest_file(manifest_path: str | Path) -> ManifestGraph:
    """Build a graph projection from a local manifest JSON file."""

    return build_manifest_graph(load_governance_manifest_file(manifest_path))


def query_manifest_graph(
    manifest: dict[str, Any],
    scope: str,
    *,
    budget_profile: str = "developer",
    limit: int | None = None,
    offset: int = 0,
) -> ManifestGraphQueryResult:
    """Return a deterministic graph slice for a repo, component, validator, projection, or ART scope."""

    graph = build_manifest_graph(manifest)
    normalized_scope = scope.strip()
    if not normalized_scope:
        raise ValueError("graph query scope must not be empty")

    node_by_id = {node.node_id: node for node in graph.nodes}
    seed_node_ids = _seed_node_ids(graph, normalized_scope)
    edge_ids = {
        edge.edge_id
        for edge in graph.edges
        if (
            edge.source_node_id in seed_node_ids
            or edge.target_node_id in seed_node_ids
            or edge.properties.get("scope") == normalized_scope
        )
    }
    selected_edges = tuple(edge for edge in graph.edges if edge.edge_id in edge_ids)
    selected_node_ids = set(seed_node_ids)
    for edge in selected_edges:
        selected_node_ids.add(edge.source_node_id)
        selected_node_ids.add(edge.target_node_id)

    selected_nodes = tuple(
        _resolve_result_node(node_id, node_by_id)
        for node_id in sorted(selected_node_ids)
        if node_id in node_by_id or node_id.startswith("scope:")
    )
    node_page, node_pagination = paginate_items(
        selected_nodes,
        limit=limit,
        offset=offset,
        operation="graph.query",
        profile=budget_profile,
    )
    edge_page, edge_pagination = paginate_items(
        tuple(sorted(selected_edges, key=lambda edge: edge.edge_id)),
        limit=limit,
        offset=offset,
        operation="graph.query",
        profile=budget_profile,
    )
    budget_decision = evaluate_operation_budget(
        "graph.query",
        observed={
            "graph_edges": len(selected_edges),
            "graph_nodes": len(selected_nodes),
            "page_limit": node_pagination.effective_limit,
        },
        profile=budget_profile,
    ).to_record()
    return ManifestGraphQueryResult(
        budget_decision=budget_decision,
        edges=edge_page,
        edge_pagination=edge_pagination.to_record(),
        manifest_id=graph.manifest_id,
        nodes=node_page,
        node_pagination=node_pagination.to_record(),
        scope=normalized_scope,
    )


def query_manifest_file(
    manifest_path: str | Path,
    scope: str,
    *,
    budget_profile: str = "developer",
    limit: int | None = None,
    offset: int = 0,
) -> ManifestGraphQueryResult:
    """Load a manifest file and query its graph projection."""

    return query_manifest_graph(
        load_governance_manifest_file(manifest_path),
        scope,
        budget_profile=budget_profile,
        limit=limit,
        offset=offset,
    )


def graph_summary(graph: ManifestGraph) -> dict[str, Any]:
    """Return a compact deterministic graph summary for API and CLI surfaces."""

    node_type_counts: dict[str, int] = {}
    edge_type_counts: dict[str, int] = {}
    for node in graph.nodes:
        node_type_counts[node.node_type] = node_type_counts.get(node.node_type, 0) + 1
    for edge in graph.edges:
        edge_type_counts[edge.edge_type] = edge_type_counts.get(edge.edge_type, 0) + 1
    return {
        "edge_count": len(graph.edges),
        "edge_type_counts": dict(sorted(edge_type_counts.items())),
        "manifest_id": graph.manifest_id,
        "node_count": len(graph.nodes),
        "node_type_counts": dict(sorted(node_type_counts.items())),
    }


def _seed_node_ids(graph: ManifestGraph, scope: str) -> set[str]:
    nodes = graph.nodes
    if scope.startswith("repo:"):
        repo_id = scope.removeprefix("repo:")
        return {
            node.node_id
            for node in nodes
            if (
                node.node_id == scope
                or node.owner_repo == repo_id
                or node.properties.get("repo_name") == repo_id
                or node.properties.get("output_ref", {}).get("repo") == repo_id
            )
        }
    if scope.startswith("component:"):
        return {node.node_id for node in nodes if node.node_id == scope}
    if scope.startswith("validator:"):
        return {node.node_id for node in nodes if node.node_id == scope}
    if scope.startswith("projection:"):
        return {node.node_id for node in nodes if node.node_id == scope}
    if scope.startswith("authority:"):
        return {node.node_id for node in nodes if node.node_id == scope}
    if scope.startswith("art:"):
        return {
            node.node_id
            for node in nodes
            if scope in node.properties.get("scopes", ())
        }
    return {node.node_id for node in nodes if node.node_id == scope}


def _resolve_result_node(
    node_id: str,
    node_by_id: dict[str, ManifestGraphNode],
) -> ManifestGraphNode:
    if node_id in node_by_id:
        return node_by_id[node_id]
    return ManifestGraphNode(
        external_ref=node_id.removeprefix("scope:"),
        node_id=node_id,
        node_type="scope",
        owner_repo=None,
        properties={
            "synthetic": True,
        },
    )
