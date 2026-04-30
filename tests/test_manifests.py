from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core.manifests import (
    MANIFEST_SCHEMA_VERSION,
    governance_manifest_schema,
    manifest_entity_ids,
    validate_governance_manifest,
)


EXAMPLE_MANIFEST_PATH = REPO_ROOT / "examples/governance-manifest.example.json"
SCHEMA_PATH = REPO_ROOT / "schemas/governance-manifest.schema.json"


def load_example_manifest() -> dict:
    return json.loads(EXAMPLE_MANIFEST_PATH.read_text(encoding="utf-8"))


class GovernanceManifestTests(TestCase):
    def test_static_schema_matches_runtime_schema(self) -> None:
        static_schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

        self.assertEqual(static_schema, governance_manifest_schema())

    def test_example_manifest_is_valid(self) -> None:
        manifest = load_example_manifest()
        result = validate_governance_manifest(manifest)

        self.assertTrue(result.valid, result.errors)
        self.assertEqual(manifest["schema_version"], MANIFEST_SCHEMA_VERSION)

    def test_manifest_entity_ids_are_stable_by_section(self) -> None:
        manifest = load_example_manifest()

        self.assertEqual(
            manifest_entity_ids(manifest),
            {
                "components": ["control-fabric-core"],
                "projections": ["workspace-repo-authority-graph"],
                "repos": ["workspace-governance-control-fabric"],
                "validators": ["control-fabric-project-scaffold"],
            },
        )

    def test_manifest_rejects_unknown_authority_references(self) -> None:
        manifest = load_example_manifest()
        manifest["validators"][0]["authority_ref_ids"].append("missing-authority")

        result = validate_governance_manifest(manifest)

        self.assertFalse(result.valid)
        self.assertIn(
            "validators[0].authority_ref_ids references unknown authority id missing-authority",
            result.errors,
        )

    def test_manifest_rejects_duplicate_entity_ids(self) -> None:
        manifest = load_example_manifest()
        manifest["repos"].append(deepcopy(manifest["repos"][0]))

        result = validate_governance_manifest(manifest)

        self.assertFalse(result.valid)
        self.assertIn(
            "repos[1].repo_id duplicates workspace-governance-control-fabric",
            result.errors,
        )

    def test_manifest_rejects_missing_required_sections(self) -> None:
        manifest = load_example_manifest()
        del manifest["projections"]

        result = validate_governance_manifest(manifest)

        self.assertFalse(result.valid)
        self.assertIn("projections must be a list", result.errors)
