from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core.graph_queries import graph_summary, query_manifest_file, query_manifest_graph
from control_fabric_core.graph_ingestion import build_manifest_graph


EXAMPLE_MANIFEST_PATH = REPO_ROOT / "examples/governance-manifest.example.json"


def load_example_manifest() -> dict:
    return json.loads(EXAMPLE_MANIFEST_PATH.read_text(encoding="utf-8"))


class ManifestGraphQueryTests(TestCase):
    def test_repo_scope_returns_repo_owned_graph_slice(self) -> None:
        result = query_manifest_file(
            EXAMPLE_MANIFEST_PATH,
            "repo:workspace-governance-control-fabric",
        )
        node_ids = {node.node_id for node in result.nodes}

        self.assertIn("repo:workspace-governance-control-fabric", node_ids)
        self.assertIn("component:control-fabric-core", node_ids)
        self.assertIn("validator:control-fabric-project-scaffold", node_ids)
        self.assertTrue(result.edges)

    def test_component_scope_returns_component_authority_context(self) -> None:
        result = query_manifest_graph(load_example_manifest(), "component:control-fabric-core")
        node_ids = {node.node_id for node in result.nodes}
        edge_types = {edge.edge_type for edge in result.edges}

        self.assertIn("component:control-fabric-core", node_ids)
        self.assertIn("authority:wgcf-operator-surface", node_ids)
        self.assertIn("declared-by-authority", edge_types)

    def test_art_scope_returns_validator_and_synthetic_scope_node(self) -> None:
        result = query_manifest_graph(load_example_manifest(), "art:delivery-420")
        node_ids = {node.node_id for node in result.nodes}

        self.assertIn("validator:control-fabric-project-scaffold", node_ids)
        self.assertIn("scope:art:delivery-420", node_ids)
        self.assertTrue(any(node.node_type == "scope" for node in result.nodes))

    def test_graph_summary_counts_node_and_edge_types(self) -> None:
        summary = graph_summary(build_manifest_graph(load_example_manifest()))

        self.assertGreater(summary["node_count"], 0)
        self.assertGreater(summary["edge_count"], 0)
        self.assertEqual(summary["node_type_counts"]["repo"], 1)

    def test_empty_query_scope_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "scope must not be empty"):
            query_manifest_graph(load_example_manifest(), " ")

    def test_graph_query_applies_budgeted_pagination(self) -> None:
        result = query_manifest_graph(
            load_example_manifest(),
            "repo:workspace-governance-control-fabric",
            limit=1,
        )
        record = result.to_record()

        self.assertEqual(record["summary"]["node_count"], 1)
        self.assertGreater(record["summary"]["node_total_count"], 1)
        self.assertEqual(record["node_pagination"]["effective_limit"], 1)
        self.assertTrue(record["node_pagination"]["has_next_page"])
        self.assertEqual(record["budget_decision"]["invocation_class"], "inline-fast")
