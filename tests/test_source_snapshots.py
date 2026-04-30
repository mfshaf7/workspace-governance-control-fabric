from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import build_source_snapshot
from control_fabric_core.source_snapshots import CORE_AUTHORITY_DECLARATIONS


RAW_MARKER = "SECRET RAW POLICY CONTENT"
SKIPPED_AUTHORITY_PATH = "docs/reviews/components/2026-04-30-workspace-governance-control-fabric-devint-runtime.md"


def write_file(root: Path, repo: str, rel_path: str, content: str) -> None:
    path = root / repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_repo_manifests(root: Path, repo: str) -> None:
    write_file(root, repo, "AGENTS.md", f"# {repo} agent guidance\n")
    write_file(root, repo, "README.md", f"# {repo}\n")


def write_workspace_fixture(root: Path) -> None:
    repos = {
        "workspace-governance-control-fabric",
        *(repo for _, repo, _ in CORE_AUTHORITY_DECLARATIONS),
    }
    for repo in repos:
        (root / repo).mkdir(parents=True, exist_ok=True)
        write_repo_manifests(root, repo)

    owner_map = {
        "components": {
            "operator-orchestration-service": {
                "owner_repo": "operator-orchestration-service",
                "interface_contract": {"path": "contracts/interface-manifest.json"},
            },
        },
        "repos": {repo: {"owner_repo": repo} for repo in sorted(repos)},
    }
    write_file(
        root,
        "workspace-governance",
        "generated/resolved-owner-map.json",
        json.dumps(owner_map, sort_keys=True),
    )

    for source_kind, repo, rel_path in CORE_AUTHORITY_DECLARATIONS:
        if rel_path == SKIPPED_AUTHORITY_PATH:
            continue
        content = f"{source_kind}:{repo}:{rel_path}\n"
        if rel_path == "contracts/governance-validator-catalog.yaml":
            content += f"{RAW_MARKER}\n"
        write_file(root, repo, rel_path, content)

    write_file(
        root,
        "workspace-governance-control-fabric",
        "examples/governance-manifest.example.json",
        "{}\n",
    )
    write_file(
        root,
        "workspace-governance-control-fabric",
        "dev-integration/profiles/governance-control-fabric/profile.yaml",
        "id: governance-control-fabric\n",
    )
    write_file(
        root,
        "workspace-governance-control-fabric",
        "dev-integration/profiles/governance-control-fabric/README.md",
        "# WGCF devint\n",
    )


class SourceSnapshotTests(TestCase):
    def test_snapshot_discovers_authority_refs_without_raw_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            write_workspace_fixture(workspace_root)

            snapshot = build_source_snapshot(workspace_root, actor="test-operator")
            record = snapshot.to_record()

        self.assertTrue(record["snapshot_id"].startswith("source-snapshot:"))
        self.assertGreaterEqual(record["summary"]["authority_ref_count"], 25)
        authority_repos = {
            source_ref["repo"]
            for source_ref in record["authority_refs"]
        }
        for repo in (
            "workspace-governance",
            "platform-engineering",
            "security-architecture",
            "operator-orchestration-service",
            "workspace-governance-control-fabric",
        ):
            self.assertIn(repo, authority_repos)

        source_kinds = {
            source_ref["source_kind"]
            for source_ref in record["authority_refs"]
        }
        for source_kind in (
            "workspace-authority",
            "validator-catalog",
            "platform-runtime",
            "security-review",
            "operator-workflow",
            "repo-manifest",
            "dev-integration-profile",
            "component-interface:operator-orchestration-service",
        ):
            self.assertIn(source_kind, source_kinds)

        serialized = json.dumps(record, sort_keys=True)
        self.assertNotIn(RAW_MARKER, serialized)
        self.assertTrue(
            any(
                excluded["path"] == SKIPPED_AUTHORITY_PATH
                and excluded["reason"] == "missing"
                for excluded in record["excluded_refs"]
            ),
        )

    def test_snapshot_digest_changes_when_authority_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            write_workspace_fixture(workspace_root)

            first = build_source_snapshot(workspace_root, actor="test-operator")
            write_file(
                workspace_root,
                "workspace-governance",
                "contracts/governance-validator-catalog.yaml",
                "changed validator catalog\n",
            )
            second = build_source_snapshot(workspace_root, actor="test-operator")

        authority_id = "validator-catalog:workspace-governance:contracts/governance-validator-catalog.yaml"
        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertNotEqual(first.digests[authority_id], second.digests[authority_id])

    def test_isolated_checkout_keeps_local_runtime_refs_and_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir)
            repo = "workspace-governance-control-fabric"
            (workspace_root / repo).mkdir(parents=True, exist_ok=True)
            write_repo_manifests(workspace_root, repo)
            write_file(
                workspace_root,
                repo,
                "dev-integration/profiles/governance-control-fabric/profile.yaml",
                "id: governance-control-fabric\n",
            )

            snapshot = build_source_snapshot(workspace_root, actor="isolated-ci")
            record = snapshot.to_record()

        authority_repos = {
            source_ref["repo"]
            for source_ref in record["authority_refs"]
        }
        source_kinds = {
            source_ref["source_kind"]
            for source_ref in record["authority_refs"]
        }
        self.assertIn("workspace-governance-control-fabric", authority_repos)
        self.assertIn("repo-manifest", source_kinds)
        self.assertIn("dev-integration-profile", source_kinds)
        self.assertGreater(record["summary"]["excluded_ref_count"], 0)
