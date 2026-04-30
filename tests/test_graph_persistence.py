from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import TestCase

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import (
    AuthoritySourceRef,
    ExcludedSourceRef,
    SourceRootSnapshot,
    SourceSnapshot,
    build_manifest_graph,
    persist_governance_state,
    persist_manifest_graph,
    persist_source_snapshot,
)
from control_fabric_core.db import (
    AuthorityReference,
    GovernanceEdge,
    GovernanceNode,
    SourceSnapshot as SourceSnapshotModel,
    metadata,
)


AUTHORITY_ID = "workspace-authority:workspace-governance:contracts/repos.yaml"


def load_example_graph():
    manifest = json.loads((REPO_ROOT / "examples/governance-manifest.example.json").read_text(encoding="utf-8"))
    return build_manifest_graph(manifest)


def sample_snapshot(snapshot_id: str = "source-snapshot:test", digest: str = "sha256:one") -> SourceSnapshot:
    return SourceSnapshot(
        actor="test-operator",
        authority_refs=(
            AuthoritySourceRef(
                authority_id=AUTHORITY_ID,
                digest=digest,
                freshness_status="current",
                path="contracts/repos.yaml",
                ref="main",
                repo="workspace-governance",
                source_kind="workspace-authority",
            ),
        ),
        excluded_refs=(
            ExcludedSourceRef(
                authority_id="security-review:missing",
                path="registers/missing.yaml",
                reason="missing",
                repo="security-architecture",
                source_kind="security-review",
            ),
        ),
        snapshot_id=snapshot_id,
        source_roots=(
            SourceRootSnapshot(
                exists=True,
                ref="main",
                repo="workspace-governance",
                root_path="/workspace/workspace-governance",
            ),
        ),
        workspace_root="/workspace",
    )


def session_factory():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class GraphPersistenceTests(TestCase):
    def test_persist_governance_state_writes_snapshot_refs_graph_and_scope_nodes(self) -> None:
        Session = session_factory()
        graph = load_example_graph()
        snapshot = sample_snapshot()

        with Session() as session:
            result = persist_governance_state(session, graph=graph, snapshot=snapshot)
            session.commit()

            stored_snapshot = session.get(SourceSnapshotModel, snapshot.snapshot_id)
            stored_authority = session.get(AuthorityReference, AUTHORITY_ID)
            synthetic_scope = session.get(GovernanceNode, "scope:art:delivery-420")
            edges = session.execute(select(GovernanceEdge)).scalars().all()
            edges_have_nodes = all(
                session.get(GovernanceNode, edge.source_node_id) is not None
                and session.get(GovernanceNode, edge.target_node_id) is not None
                for edge in edges
            )

        self.assertEqual(result.source_snapshot.snapshot_id, snapshot.snapshot_id)
        self.assertEqual(result.source_snapshot.authority_ref_count, 1)
        self.assertEqual(result.graph.edge_count, len(graph.edges))
        self.assertGreaterEqual(result.graph.node_count, len(graph.nodes))
        self.assertGreaterEqual(result.graph.synthetic_node_count, 1)
        self.assertIsNotNone(stored_snapshot)
        self.assertEqual(stored_snapshot.digests[AUTHORITY_ID], "sha256:one")
        self.assertIsNotNone(stored_authority)
        self.assertEqual(stored_authority.freshness_status, "current")
        self.assertEqual(stored_authority.digest, "sha256:one")
        self.assertIsNotNone(synthetic_scope)
        self.assertEqual(synthetic_scope.node_type, "scope")
        self.assertTrue(edges_have_nodes)

    def test_persistence_helpers_are_idempotent_and_update_authority_digest(self) -> None:
        Session = session_factory()
        graph = load_example_graph()

        with Session() as session:
            first = persist_source_snapshot(session, sample_snapshot("source-snapshot:first", "sha256:first"))
            second = persist_source_snapshot(session, sample_snapshot("source-snapshot:second", "sha256:second"))
            first_graph = persist_manifest_graph(session, graph)
            second_graph = persist_manifest_graph(session, graph)
            session.commit()

            authority = session.get(AuthorityReference, AUTHORITY_ID)
            snapshot_count = session.scalar(select(func.count()).select_from(SourceSnapshotModel))
            node_count = session.scalar(select(func.count()).select_from(GovernanceNode))
            edge_count = session.scalar(select(func.count()).select_from(GovernanceEdge))

        self.assertEqual(first.authority_ref_count, 1)
        self.assertEqual(second.authority_ref_count, 1)
        self.assertEqual(first_graph.to_record(), second_graph.to_record())
        self.assertEqual(authority.digest, "sha256:second")
        self.assertEqual(snapshot_count, 2)
        self.assertEqual(node_count, second_graph.node_count)
        self.assertEqual(edge_count, second_graph.edge_count)
