"""Persistence helpers for fabric-local source snapshots and graph records.

The database stores WGCF implementation state only. These helpers persist
digests, refs, graph nodes, and graph edges without becoming an authority
source and without committing the caller's transaction.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.orm import Session

from .db.models import (
    AuthorityReference as AuthorityReferenceModel,
    GovernanceEdge as GovernanceEdgeModel,
    GovernanceNode as GovernanceNodeModel,
    SourceSnapshot as SourceSnapshotModel,
)
from .graph_ingestion import ManifestGraph, ManifestGraphNode
from .source_snapshots import SourceSnapshot as SourceSnapshotRecord


@dataclass(frozen=True)
class SourceSnapshotPersistenceResult:
    """Compact result for one persisted source snapshot."""

    authority_ref_count: int
    excluded_ref_count: int
    snapshot_id: str
    source_root_count: int

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GraphPersistenceResult:
    """Compact result for one persisted manifest graph."""

    edge_count: int
    manifest_id: str
    node_count: int
    synthetic_node_count: int

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GovernanceStatePersistenceResult:
    """Combined result for persisted snapshot plus graph state."""

    graph: GraphPersistenceResult
    source_snapshot: SourceSnapshotPersistenceResult

    def to_record(self) -> dict[str, Any]:
        return {
            "graph": self.graph.to_record(),
            "source_snapshot": self.source_snapshot.to_record(),
        }


def persist_source_snapshot(
    session: Session,
    snapshot: SourceSnapshotRecord,
) -> SourceSnapshotPersistenceResult:
    """Persist a source snapshot and digest-linked authority refs.

    The function is idempotent by primary key and flushes but does not commit.
    """

    record = snapshot.to_record()
    for source_ref in snapshot.authority_refs:
        session.merge(
            AuthorityReferenceModel(
                authority_id=source_ref.authority_id,
                digest=source_ref.digest,
                freshness_status=source_ref.freshness_status,
                path=source_ref.path,
                ref=source_ref.ref,
                repo=source_ref.repo,
            ),
        )
    session.merge(
        SourceSnapshotModel(
            actor=snapshot.actor,
            authority_refs=record["authority_refs"],
            digests=record["digests"],
            excluded_refs=record["excluded_refs"],
            snapshot_id=snapshot.snapshot_id,
            source_roots=record["source_roots"],
        ),
    )
    session.flush()
    return SourceSnapshotPersistenceResult(
        authority_ref_count=len(snapshot.authority_refs),
        excluded_ref_count=len(snapshot.excluded_refs),
        snapshot_id=snapshot.snapshot_id,
        source_root_count=len(snapshot.source_roots),
    )


def persist_manifest_graph(
    session: Session,
    graph: ManifestGraph,
) -> GraphPersistenceResult:
    """Persist manifest graph nodes and edges idempotently.

    Edges may point at synthetic scope nodes used for ART-oriented planning.
    Those nodes are materialized locally so relational integrity remains true.
    """

    nodes = _graph_nodes_with_edge_targets(graph)
    synthetic_node_count = 0
    for node in nodes:
        if node.properties.get("synthetic"):
            synthetic_node_count += 1
        session.merge(
            GovernanceNodeModel(
                external_ref=node.external_ref,
                node_id=node.node_id,
                node_type=node.node_type,
                owner_repo=node.owner_repo,
                properties=node.properties,
            ),
        )
    session.flush()
    for edge in graph.edges:
        session.merge(
            GovernanceEdgeModel(
                edge_id=edge.edge_id,
                edge_type=edge.edge_type,
                properties=edge.properties,
                source_node_id=edge.source_node_id,
                target_node_id=edge.target_node_id,
            ),
        )
    session.flush()
    return GraphPersistenceResult(
        edge_count=len(graph.edges),
        manifest_id=graph.manifest_id,
        node_count=len(nodes),
        synthetic_node_count=synthetic_node_count,
    )


def persist_governance_state(
    session: Session,
    *,
    graph: ManifestGraph,
    snapshot: SourceSnapshotRecord,
) -> GovernanceStatePersistenceResult:
    """Persist source snapshot and manifest graph state in caller transaction."""

    source_result = persist_source_snapshot(session, snapshot)
    graph_result = persist_manifest_graph(session, graph)
    return GovernanceStatePersistenceResult(
        graph=graph_result,
        source_snapshot=source_result,
    )


def _graph_nodes_with_edge_targets(graph: ManifestGraph) -> tuple[ManifestGraphNode, ...]:
    nodes = {node.node_id: node for node in graph.nodes}
    for edge in graph.edges:
        for node_id in (edge.source_node_id, edge.target_node_id):
            if node_id not in nodes:
                nodes[node_id] = _synthetic_node(node_id, graph.manifest_id)
    return tuple(nodes[key] for key in sorted(nodes))


def _synthetic_node(node_id: str, manifest_id: str) -> ManifestGraphNode:
    if node_id.startswith("scope:"):
        return ManifestGraphNode(
            external_ref=node_id.removeprefix("scope:"),
            node_id=node_id,
            node_type="scope",
            owner_repo=None,
            properties={
                "manifest_id": manifest_id,
                "synthetic": True,
            },
        )
    return ManifestGraphNode(
        external_ref=node_id,
        node_id=node_id,
        node_type="unresolved-reference",
        owner_repo=None,
        properties={
            "manifest_id": manifest_id,
            "synthetic": True,
        },
    )
