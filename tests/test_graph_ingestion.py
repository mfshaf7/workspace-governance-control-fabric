from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core.graph_ingestion import build_manifest_graph


EXAMPLE_MANIFEST_PATH = REPO_ROOT / "examples/governance-manifest.example.json"


def load_example_manifest() -> dict:
    return json.loads(EXAMPLE_MANIFEST_PATH.read_text(encoding="utf-8"))


class ManifestGraphIngestionTests(TestCase):
    def test_manifest_graph_contains_authority_and_runtime_nodes(self) -> None:
        graph = build_manifest_graph(load_example_manifest())
        nodes_by_id = {node.node_id: node for node in graph.nodes}

        self.assertEqual(graph.manifest_id, "wgcf-bootstrap-manifest")
        self.assertEqual(
            {
                "authority-reference",
                "component",
                "projection",
                "repo",
                "validator",
            },
            {node.node_type for node in graph.nodes},
        )
        self.assertEqual(
            nodes_by_id["authority:wgcf-operator-surface"].properties["digest"],
            "sha256:example-operator-surface",
        )
        self.assertEqual(
            nodes_by_id["repo:workspace-governance-control-fabric"].owner_repo,
            "workspace-governance-control-fabric",
        )

    def test_manifest_graph_preserves_relationships_as_edges(self) -> None:
        graph = build_manifest_graph(load_example_manifest())
        edge_tuples = {
            (edge.source_node_id, edge.edge_type, edge.target_node_id)
            for edge in graph.edges
        }

        self.assertIn(
            (
                "repo:workspace-governance-control-fabric",
                "declared-by-authority",
                "authority:wgcf-operator-surface",
            ),
            edge_tuples,
        )
        self.assertIn(
            (
                "component:control-fabric-core",
                "owned-by-repo",
                "repo:workspace-governance-control-fabric",
            ),
            edge_tuples,
        )
        self.assertIn(
            (
                "validator:control-fabric-project-scaffold",
                "validates-scope",
                "repo:workspace-governance-control-fabric",
            ),
            edge_tuples,
        )
        self.assertIn(
            (
                "projection:workspace-repo-authority-graph",
                "projects-from-authority",
                "authority:workspace-repo-contracts",
            ),
            edge_tuples,
        )
        self.assertIn(
            (
                "projection:workspace-repo-authority-graph",
                "emits-to-repo",
                "repo:workspace-governance-control-fabric",
            ),
            edge_tuples,
        )

    def test_manifest_graph_output_is_record_shaped(self) -> None:
        records = build_manifest_graph(load_example_manifest()).to_records()

        self.assertEqual(set(records), {"edges", "nodes"})
        self.assertTrue(all("node_id" in node for node in records["nodes"]))
        self.assertTrue(all("edge_id" in edge for edge in records["edges"]))

    def test_manifest_graph_rejects_invalid_manifest(self) -> None:
        manifest = deepcopy(load_example_manifest())
        manifest["components"][0]["authority_ref_ids"] = ["missing-authority"]

        with self.assertRaisesRegex(ValueError, "missing-authority"):
            build_manifest_graph(manifest)
