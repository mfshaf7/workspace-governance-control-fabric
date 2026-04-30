from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import build_validation_plan, normalize_validation_target
from control_fabric_core.validation_planning import ValidationTier


EXAMPLE_MANIFEST_PATH = REPO_ROOT / "examples/governance-manifest.example.json"


def load_example_manifest() -> dict:
    return json.loads(EXAMPLE_MANIFEST_PATH.read_text(encoding="utf-8"))


class ValidationPlanningTests(TestCase):
    def test_scoped_repo_plan_includes_smoke_and_scoped_checks(self) -> None:
        plan = build_validation_plan(
            load_example_manifest(),
            "repo:workspace-governance-control-fabric",
            tier="scoped",
        )

        self.assertEqual(plan.decision.outcome, "planned")
        self.assertFalse(plan.decision.requires_operator_review)
        self.assertEqual(plan.target.target_type, "repo")
        self.assertEqual(
            [check.validator_id for check in plan.checks],
            [
                "control-fabric-project-scaffold",
                "control-fabric-status-smoke",
            ],
        )
        self.assertEqual({check.tier for check in plan.checks}, {"scoped", "smoke"})

    def test_smoke_repo_plan_excludes_scoped_check(self) -> None:
        plan = build_validation_plan(
            load_example_manifest(),
            "repo:workspace-governance-control-fabric",
            tier=ValidationTier.SMOKE,
        )

        self.assertEqual(plan.decision.outcome, "planned")
        self.assertEqual(
            [check.validator_id for check in plan.checks],
            ["control-fabric-status-smoke"],
        )
        self.assertEqual(
            [item.validator_id for item in plan.decision.suppressed_validators],
            ["control-fabric-project-scaffold"],
        )

    def test_component_plan_uses_manifest_declared_component_scope(self) -> None:
        plan = build_validation_plan(
            load_example_manifest(),
            "component:control-fabric-core",
            tier="scoped",
        )

        self.assertEqual(plan.decision.outcome, "planned")
        self.assertEqual(
            [check.validator_id for check in plan.checks],
            ["control-fabric-project-scaffold"],
        )

    def test_art_plan_uses_manifest_declared_art_scope(self) -> None:
        plan = build_validation_plan(load_example_manifest(), "art:delivery-420", tier="scoped")

        self.assertEqual(plan.decision.outcome, "planned")
        self.assertEqual(plan.checks[0].validator_id, "control-fabric-project-scaffold")

    def test_full_workspace_plan_includes_every_manifest_validator(self) -> None:
        plan = build_validation_plan(load_example_manifest(), "workspace", tier="full")

        self.assertEqual(plan.decision.outcome, "planned")
        self.assertIn(
            "full-surface tier includes every manifest-declared validator",
            plan.decision.reasons,
        )
        self.assertEqual(len(plan.checks), 2)

    def test_unknown_scope_requires_operator_review(self) -> None:
        plan = build_validation_plan(load_example_manifest(), "repo:missing-repo", tier="scoped")

        self.assertEqual(plan.decision.outcome, "no_matching_validators")
        self.assertTrue(plan.decision.requires_operator_review)
        self.assertIn("target scope is not declared by the manifest graph", plan.decision.reasons)
        self.assertEqual(plan.checks, ())

    def test_release_plan_blocks_on_stale_authority_ref(self) -> None:
        manifest = deepcopy(load_example_manifest())
        manifest["authority_refs"][0]["freshness_status"] = "stale"

        plan = build_validation_plan(manifest, "workspace", tier="release")

        self.assertEqual(plan.decision.outcome, "blocked")
        self.assertTrue(plan.decision.requires_operator_review)
        self.assertEqual(len(plan.checks), 2)
        self.assertIn("wgcf-operator-surface", plan.decision.blocked_reasons[0])

    def test_plan_id_is_deterministic(self) -> None:
        manifest = load_example_manifest()

        first = build_validation_plan(manifest, "art:delivery-420", tier="scoped")
        second = build_validation_plan(manifest, "art:delivery-420", tier="scoped")

        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(first.to_record(), second.to_record())

    def test_target_scope_validation_is_explicit(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            normalize_validation_target(" ")

        with self.assertRaisesRegex(ValueError, "must be workspace or start with"):
            normalize_validation_target("delivery-420")
