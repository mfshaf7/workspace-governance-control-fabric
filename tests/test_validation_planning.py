from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import (
    build_validation_plan,
    normalize_validation_target,
    validation_target_scope_candidates,
)
from control_fabric_core.validation_planning import ValidationTier


EXAMPLE_MANIFEST_PATH = REPO_ROOT / "examples/governance-manifest.example.json"


def load_example_manifest() -> dict:
    return json.loads(EXAMPLE_MANIFEST_PATH.read_text(encoding="utf-8"))


def authority_digests(manifest: dict) -> dict[str, str]:
    return {
        authority_ref["authority_id"]: authority_ref["digest"]
        for authority_ref in manifest["authority_refs"]
        if authority_ref.get("digest")
    }


class ValidationPlanningTests(TestCase):
    def test_scoped_repo_plan_includes_smoke_and_scoped_checks(self) -> None:
        plan = build_validation_plan(
            load_example_manifest(),
            "repo:workspace-governance-control-fabric",
            tier="scoped",
        )

        self.assertEqual(plan.decision.outcome, "planned")
        self.assertFalse(plan.decision.requires_operator_review)
        self.assertEqual(plan.performance_budget["invocation_class"], "inline-fast")
        self.assertTrue(plan.performance_budget["within_budget"])
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

    def test_changed_file_plan_expands_to_repo_and_component_scopes(self) -> None:
        plan = build_validation_plan(
            load_example_manifest(),
            "changed-file:packages/control_fabric_core/src/control_fabric_core/validation_planning.py",
            tier="scoped",
        )

        self.assertEqual(plan.decision.outcome, "planned")
        self.assertEqual(plan.target.target_type, "changed-file")
        self.assertEqual(
            [check.validator_id for check in plan.checks],
            [
                "control-fabric-project-scaffold",
                "control-fabric-status-smoke",
            ],
        )
        self.assertIn(
            "changed-file target expands to "
            "component:control-fabric-core, repo:workspace-governance-control-fabric",
            plan.decision.reasons,
        )

    def test_changed_file_candidates_include_profile_release_and_impact_scopes(self) -> None:
        manifest = deepcopy(load_example_manifest())
        manifest["repos"][0]["source_paths"] = ["packages", "dev-integration", "deployments"]
        manifest["repos"][0]["impact_scopes"] = ["release:wgcf-devint"]
        manifest["components"][0]["impact_scopes"] = ["art:delivery-498"]

        candidates = validation_target_scope_candidates(
            manifest,
            "changed-file:packages/control_fabric_core/src/control_fabric_core/validation_planning.py",
        )

        self.assertEqual(
            candidates,
            (
                "art:delivery-498",
                "changed-file:packages/control_fabric_core/src/control_fabric_core/validation_planning.py",
                "component:control-fabric-core",
                "release:wgcf-devint",
                "repo:workspace-governance-control-fabric",
            ),
        )

        profile_candidates = validation_target_scope_candidates(
            manifest,
            "changed-file:dev-integration/profiles/governance-control-fabric/profile.yaml",
        )

        self.assertIn("profile:governance-control-fabric", profile_candidates)
        self.assertIn("release:wgcf-devint", profile_candidates)

    def test_profile_and_release_targets_select_matching_validators(self) -> None:
        manifest = deepcopy(load_example_manifest())
        manifest["validators"].extend(
            [
                {
                    "validator_id": "profile-runtime-smoke",
                    "owner_repo": "workspace-governance-control-fabric",
                    "command": "python3 scripts/validate_project.py --repo-root .",
                    "scopes": ["profile:governance-control-fabric"],
                    "validation_tier": "smoke",
                    "check_type": "command",
                    "required": True,
                    "authority_ref_ids": ["wgcf-runtime-repo-guidance"],
                },
                {
                    "validator_id": "release-readiness-smoke",
                    "owner_repo": "workspace-governance-control-fabric",
                    "command": "python3 scripts/validate_project.py --repo-root .",
                    "scopes": ["release:wgcf-devint"],
                    "validation_tier": "smoke",
                    "check_type": "command",
                    "required": True,
                    "authority_ref_ids": ["wgcf-runtime-repo-guidance"],
                },
            ],
        )

        profile_plan = build_validation_plan(manifest, "profile:governance-control-fabric", tier="smoke")
        release_plan = build_validation_plan(manifest, "release:wgcf-devint", tier="smoke")

        self.assertEqual([check.validator_id for check in profile_plan.checks], ["profile-runtime-smoke"])
        self.assertEqual([check.validator_id for check in release_plan.checks], ["release-readiness-smoke"])

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

    def test_fresh_receipt_marks_safe_check_as_skip_candidate(self) -> None:
        manifest = load_example_manifest()
        plan = build_validation_plan(
            manifest,
            "repo:workspace-governance-control-fabric",
            tier="scoped",
            receipts=[
                {
                    "authority_ref_digests": authority_digests(manifest),
                    "receipt_id": "receipt:project-scaffold:1",
                    "validator_id": "control-fabric-project-scaffold",
                    "target_scope": "repo:workspace-governance-control-fabric",
                    "tier": "scoped",
                    "status": "success",
                    "captured_at": "2026-04-30T09:00:00Z",
                    "digest": "sha256:example-project-scaffold",
                },
            ],
            now="2026-04-30T09:05:00Z",
        )

        checks = {check.validator_id: check for check in plan.checks}
        reused = checks["control-fabric-project-scaffold"]
        self.assertEqual(reused.execution_mode, "skip_fresh_receipt")
        self.assertEqual(reused.receipt_id, "receipt:project-scaffold:1")
        self.assertEqual(reused.receipt_digest, "sha256:example-project-scaffold")
        self.assertEqual(reused.cache_decision["action"], "reuse")
        self.assertIn("fresh_receipts_applied=1", plan.decision.reasons)
        self.assertEqual(checks["control-fabric-status-smoke"].execution_mode, "run")

    def test_stale_receipt_does_not_skip_validation(self) -> None:
        manifest = load_example_manifest()
        plan = build_validation_plan(
            manifest,
            "repo:workspace-governance-control-fabric",
            tier="scoped",
            receipts=[
                {
                    "authority_ref_digests": authority_digests(manifest),
                    "receipt_id": "receipt:project-scaffold:old",
                    "validator_id": "control-fabric-project-scaffold",
                    "target_scope": "repo:workspace-governance-control-fabric",
                    "tier": "scoped",
                    "status": "success",
                    "captured_at": "2026-04-30T08:00:00Z",
                },
            ],
            now="2026-04-30T09:05:00Z",
        )

        self.assertEqual(
            {check.validator_id: check.execution_mode for check in plan.checks},
            {
                "control-fabric-project-scaffold": "run",
                "control-fabric-status-smoke": "run",
            },
        )

    def test_receipt_reuse_requires_current_authority_digests(self) -> None:
        manifest = load_example_manifest()
        stale_digests = authority_digests(manifest)
        stale_digests["wgcf-operator-surface"] = "sha256:old-authority"

        plan = build_validation_plan(
            manifest,
            "repo:workspace-governance-control-fabric",
            tier="scoped",
            receipts=[
                {
                    "authority_ref_digests": stale_digests,
                    "receipt_id": "receipt:project-scaffold:stale-authority",
                    "validator_id": "control-fabric-project-scaffold",
                    "target_scope": "repo:workspace-governance-control-fabric",
                    "tier": "scoped",
                    "status": "success",
                    "captured_at": "2026-04-30T09:00:00Z",
                    "digest": "sha256:stale-authority",
                },
            ],
            now="2026-04-30T09:05:00Z",
        )

        check = next(item for item in plan.checks if item.validator_id == "control-fabric-project-scaffold")
        self.assertEqual(check.execution_mode, "run")
        self.assertIn("authority digest changed", check.cache_decision["reason"])

    def test_newest_matching_receipt_is_reused(self) -> None:
        manifest = load_example_manifest()
        plan = build_validation_plan(
            manifest,
            "repo:workspace-governance-control-fabric",
            tier="scoped",
            receipts=[
                {
                    "authority_ref_digests": authority_digests(manifest),
                    "receipt_id": "receipt:project-scaffold:older",
                    "validator_id": "control-fabric-project-scaffold",
                    "target_scope": "repo:workspace-governance-control-fabric",
                    "tier": "scoped",
                    "status": "success",
                    "captured_at": "2026-04-30T09:00:00Z",
                    "digest": "sha256:older",
                },
                {
                    "authority_ref_digests": authority_digests(manifest),
                    "receipt_id": "receipt:project-scaffold:newer",
                    "validator_id": "control-fabric-project-scaffold",
                    "target_scope": "repo:workspace-governance-control-fabric",
                    "tier": "scoped",
                    "status": "success",
                    "captured_at": "2026-04-30T09:04:00Z",
                    "digest": "sha256:newer",
                },
            ],
            now="2026-04-30T09:05:00Z",
        )

        check = next(item for item in plan.checks if item.validator_id == "control-fabric-project-scaffold")
        self.assertEqual(check.execution_mode, "skip_fresh_receipt")
        self.assertEqual(check.receipt_id, "receipt:project-scaffold:newer")

    def test_plan_returns_selected_suppressed_external_and_waived_statuses(self) -> None:
        manifest = load_example_manifest()
        manifest["validators"].extend(
            [
                {
                    "validator_id": "external-owner-smoke",
                    "owner_repo": "security-architecture",
                    "command": "python3 scripts/validate_project.py --repo-root .",
                    "scopes": ["repo:workspace-governance-control-fabric"],
                    "validation_tier": "smoke",
                    "check_type": "command",
                    "required": True,
                    "authority_ref_ids": ["wgcf-runtime-repo-guidance"],
                },
                {
                    "validator_id": "waived-smoke",
                    "owner_repo": "workspace-governance-control-fabric",
                    "command": "python3 scripts/validate_project.py --repo-root .",
                    "scopes": ["repo:workspace-governance-control-fabric"],
                    "validation_tier": "smoke",
                    "check_type": "command",
                    "required": True,
                    "authority_ref_ids": ["wgcf-runtime-repo-guidance"],
                },
            ],
        )

        plan = build_validation_plan(
            manifest,
            "repo:workspace-governance-control-fabric",
            tier="smoke",
            waivers=[
                {
                    "expires_at": "2026-05-01T00:00:00Z",
                    "status": "approved",
                    "validator_id": "waived-smoke",
                    "waiver_id": "waiver:waived-smoke",
                },
            ],
            now="2026-04-30T00:00:00Z",
        )

        statuses = {item.validator_id: item.status for item in plan.check_statuses}
        self.assertEqual(statuses["control-fabric-status-smoke"], "selected")
        self.assertEqual(statuses["control-fabric-project-scaffold"], "suppressed")
        self.assertEqual(statuses["external-owner-smoke"], "external-owner-required")
        self.assertEqual(statuses["waived-smoke"], "waived")

    def test_plan_returns_stale_and_failed_cache_statuses(self) -> None:
        manifest = load_example_manifest()
        manifest["validators"].extend(
            [
                {
                    "validator_id": "stale-cache-smoke",
                    "owner_repo": "workspace-governance-control-fabric",
                    "command": "python3 scripts/validate_project.py --repo-root .",
                    "scopes": ["repo:workspace-governance-control-fabric"],
                    "validation_tier": "smoke",
                    "check_type": "command",
                    "required": True,
                    "reuse_policy": {"safe_to_reuse": True, "freshness_seconds": 300},
                    "authority_ref_ids": ["wgcf-runtime-repo-guidance"],
                },
                {
                    "validator_id": "failed-cache-smoke",
                    "owner_repo": "workspace-governance-control-fabric",
                    "command": "python3 scripts/validate_project.py --repo-root .",
                    "scopes": ["repo:workspace-governance-control-fabric"],
                    "validation_tier": "smoke",
                    "check_type": "command",
                    "required": True,
                    "reuse_policy": {"safe_to_reuse": True, "freshness_seconds": 300},
                    "authority_ref_ids": ["wgcf-runtime-repo-guidance"],
                },
            ],
        )

        plan = build_validation_plan(
            manifest,
            "repo:workspace-governance-control-fabric",
            tier="smoke",
            receipts=[
                {
                    "authority_ref_digests": authority_digests(manifest),
                    "captured_at": "2026-04-30T00:00:00Z",
                    "digest": "sha256:stale-cache",
                    "receipt_id": "receipt:stale-cache",
                    "status": "success",
                    "target_scope": "repo:workspace-governance-control-fabric",
                    "tier": "smoke",
                    "validator_id": "stale-cache-smoke",
                },
                {
                    "authority_ref_digests": authority_digests(manifest),
                    "captured_at": "2026-04-30T00:09:00Z",
                    "digest": "sha256:failed-cache",
                    "receipt_id": "receipt:failed-cache",
                    "status": "failure",
                    "target_scope": "repo:workspace-governance-control-fabric",
                    "tier": "smoke",
                    "validator_id": "failed-cache-smoke",
                },
            ],
            now="2026-04-30T00:10:00Z",
        )

        statuses = {item.validator_id: item.status for item in plan.check_statuses}
        self.assertEqual(statuses["stale-cache-smoke"], "stale")
        self.assertEqual(statuses["failed-cache-smoke"], "failed")

    def test_release_block_returns_blocked_check_statuses(self) -> None:
        manifest = load_example_manifest()
        manifest["authority_refs"][0]["freshness_status"] = "stale"

        plan = build_validation_plan(manifest, "workspace", tier="release")

        self.assertEqual(plan.decision.outcome, "blocked")
        self.assertEqual(
            {item.status for item in plan.check_statuses},
            {"blocked"},
        )

    def test_target_scope_validation_is_explicit(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            normalize_validation_target(" ")

        with self.assertRaisesRegex(ValueError, "must be workspace or start with"):
            normalize_validation_target("delivery-420")

        with self.assertRaisesRegex(ValueError, "relative repo path"):
            normalize_validation_target("changed-file:/etc/passwd")

        with self.assertRaisesRegex(ValueError, "relative repo path"):
            normalize_validation_target("changed-file:../outside")
