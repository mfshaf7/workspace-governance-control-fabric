"""Exact existing source-snapshot consumer; isolated Git and authority seams."""
from datetime import datetime, timedelta, timezone
import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from control_fabric_core.product_publication_contracts import (
    ProductPublicationAuthority, ProductPublicationContracts, ProductPublicationUnavailable,
)


class RegisteredOwnerPublicationTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent / "sample-owner"
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Synthetic owner")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / "README.md").write_text("Synthetic source only\n")
        self.git("add", ".")
        self.git("commit", "-m", "native source fixture")
        self.head = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.head)
        self.git("remote", "add", "origin", "https://github.com/synthetic/sample-owner.git")
        self.provider = {"provider": "github", "provider_repository_id": "42", "full_name": "synthetic/sample-owner"}
        owner = {"posture": "active", "lifecycle": "active", "requires_security_bindings": False,
            "owns": ["synthetic application"], "must_not_own": ["shared authority"],
            "validation_behavior": {"posture": "covered-by-owner-repo", "wgcf_graph_role": "product-runtime-source"},
            "provider_repository": self.provider,
            "record": {"id": "repo:sample-owner", "version": 1, "lineage": {"source": "workspace-intake",
                "source_ref": "repo:sample-owner", "intake_entry_version": 1}}}
        self.products = {"products": {"sample-app": {"posture": "active", "source_owners": ["sample-owner"]}}}
        self.records = {"repos": {"repos": {"sample-owner": owner}, "retired_repos": {}},
            "intake-register": {"repos": {}}, "products": self.products}
        now = datetime.now(timezone.utc)
        self.marker = {"schema_version": 1, "owner_repo": "sample-owner", "head": self.head,
            "provider_repository": self.provider, "observed_at": now.isoformat(),
            "valid_until": (now + timedelta(minutes=5)).isoformat()}
        self.marker_path = self.root / ".git" / "console-intake-owner-source.json"
        self.write_marker()
        self.authority = ProductPublicationAuthority(self.parent / "workspace-governance", {},
            ProductPublicationContracts.load(), source_owner_parent=self.parent)
        self.revision = "a" * 40

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.root), *args],
            text=True, stderr=subprocess.DEVNULL).strip()

    def write_marker(self):
        self.marker_path.write_text(json.dumps(self.marker))

    def read(self):
        with patch("control_fabric_core.product_publication_contracts.InventoryAuthority.snapshot",
                   return_value=SimpleNamespace(revision=self.revision, records=self.records)):
            return self.authority._registered_owner_root("sample-owner", self.products, "a" * 40)

    def test_fresh_attributed_inventory_owner_resolves_only_beneath_fixed_platform_parent(self):
        self.assertEqual(self.read(), self.root)
        original = self.root.stat().st_ino
        self.assertEqual(self.read(), self.root)
        self.assertEqual(self.root.stat().st_ino, original)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_intake_unregistered_suspended_retired_shared_or_wrong_product_owner_is_denied(self):
        saved = copy.deepcopy(self.records)
        for mutate in [
            lambda r: r["repos"]["repos"].clear(),
            lambda r: r["intake-register"]["repos"].update({"sample-owner": {"status": "admitted"}}),
            lambda r: r["repos"]["repos"]["sample-owner"].update(posture="suspended"),
            lambda r: r["repos"]["repos"]["sample-owner"].update(lifecycle="retired"),
            lambda r: r["repos"]["repos"]["sample-owner"].update(requires_security_bindings=True),
            lambda r: r["repos"]["repos"]["sample-owner"]["record"]["lineage"].update(source="legacy-migration"),
            lambda r: r["repos"]["repos"].update({"conflicting-owner": {"provider_repository": self.provider}}),
        ]:
            self.records = copy.deepcopy(saved)
            mutate(self.records)
            with self.assertRaises(ProductPublicationUnavailable): self.read()
        self.records = saved
        self.products["products"]["sample-app"]["source_owners"] = ["another-owner"]
        with self.assertRaises(ProductPublicationUnavailable): self.read()

    def test_presence_only_expired_foreign_provider_and_future_marker_are_not_current_authority(self):
        original = copy.deepcopy(self.marker)
        for mutate in [
            lambda m: m.pop("valid_until"),
            lambda m: m.update(schema_version=True),
            lambda m: m.update(owner_repo="another-owner"),
            lambda m: m.update(head="b" * 40),
            lambda m: m["provider_repository"].update(provider_repository_id="99"),
            lambda m: m.update(valid_until=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()),
            lambda m: m.update(valid_until=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()),
            lambda m: m.update(observed_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()),
        ]:
            self.marker = copy.deepcopy(original)
            mutate(self.marker)
            self.write_marker()
            with self.assertRaises(ProductPublicationUnavailable): self.read()

    def test_dirty_ignored_unattributed_symlink_or_arbitrary_source_is_preserved_and_denied(self):
        (self.root / "unproven.txt").write_text("preserve")
        with self.assertRaises(ProductPublicationUnavailable): self.read()
        self.assertEqual((self.root / "unproven.txt").read_text(), "preserve")
        (self.root / "unproven.txt").unlink()
        self.marker_path.unlink()
        with self.assertRaises(ProductPublicationUnavailable): self.read()
        self.marker_path.symlink_to(self.root / "README.md")
        with self.assertRaises(ProductPublicationUnavailable): self.read()
        self.assertTrue(self.marker_path.is_symlink())
