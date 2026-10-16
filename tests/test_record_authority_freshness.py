"""Native Git proof for deployment-owned record freshness (no provider mocks)."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from control_fabric_core.record_authority_freshness import require_record_authority_freshness
from control_fabric_core.workspace_intake_contracts import IntakeAuthority, IntakeUnavailable
from control_fabric_core.workspace_inventory_contracts import InventoryAuthority, InventoryUnavailable
from control_fabric_core.prototype_landing_contracts import PrototypeLandingAuthority, PrototypeLandingUnavailable
from control_fabric_core.prototype_maturity_contracts import PrototypeMaturityAuthority, PrototypeMaturityUnavailable
from control_fabric_core.prototype_closure_authority import PrototypeClosureAuthority, PrototypeClosureUnavailable
from control_fabric_core.product_publication_contracts import ProductPublicationAuthority, ProductPublicationUnavailable
from control_fabric_core.repository_readiness import RepositoryReadinessService, RepositoryReadinessUnavailable


class RecordAuthorityFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = datetime.now(timezone.utc)
        self.env = {**os.environ, "WGCF_RECORD_AUTHORITY_FRESHNESS_REQUIRED": "true"}
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Synthetic test author")
        self.git("config", "user.email", "test@example.invalid")
        (self.root / "records.json").write_text("{}")
        self.git("add", ".")
        self.git("commit", "-m", "reviewed source")
        self.reviewed = self.git("rev-parse", "HEAD")
        self.revision = self.reviewed
        self.git("update-ref", "refs/remotes/origin/main", self.revision)
        self.file = self.root / ".git" / "console-record-authority.json"
        self.value = dict(schema_version=1, owner_repo="workspace-governance",
            trusted_ref="refs/remotes/origin/main", reviewed_revision=self.reviewed,
            record_revision=self.revision, observed_at=self.now.isoformat(),
            valid_until=(self.now + timedelta(minutes=5)).isoformat())

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args],
            check=True, capture_output=True, timeout=10).stdout.decode().strip()

    def check(self, revision=True, **options):
        return require_record_authority_freshness(self.root, "workspace-governance",
            self.revision if revision else None, env=self.env, now=self.now, **options)

    def save(self):
        self.file.write_text(json.dumps(self.value))

    def test_optional_lane_does_not_read_or_write_git(self):
        self.assertIsNone(require_record_authority_freshness(Path("/nonexistent"), "workspace-governance", env={}))

    def test_fresh_ancestor_record_advance_is_read_only_and_old_snapshot_denied(self):
        self.save()
        before = self.git("status", "--porcelain")
        self.assertEqual(self.check(), self.reviewed)
        self.assertEqual(self.git("status", "--porcelain"), before)
        (self.root / "records.json").write_text('{"record":"approved"}')
        self.git("add", ".")
        self.git("commit", "-m", "reviewed business record")
        old_revision = self.revision
        self.revision = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.revision)
        with self.assertRaisesRegex(RuntimeError, "stale or inconsistent"):
            self.check()
        self.value["record_revision"] = self.revision
        self.save()
        self.assertEqual(self.check(), self.revision)
        self.assertEqual(self.check(revision=False), self.revision)
        with self.assertRaises(RuntimeError):
            require_record_authority_freshness(self.root, "workspace-governance", old_revision, env=self.env, now=self.now)

    def test_absent_malformed_symlink_oversized_expired_wrong_owner_ref_and_revision_deny(self):
        with self.assertRaises(RuntimeError):
            self.check()
        for value in [None, [], "invalid", {**self.value, "schema_version": True},
            {**self.value, "owner_repo": "workspace-prototype-studio"},
            {**self.value, "valid_until": self.now.isoformat()},
            {**self.value, "valid_until": (self.now + timedelta(hours=1)).isoformat()},
            {**self.value, "observed_at": "not-a-date"},
            {**self.value, "observed_at": (self.now + timedelta(minutes=1)).isoformat()},
            {**self.value, "trusted_ref": "HEAD"}, {**self.value, "record_revision": "f" * 40},
            {**self.value, "reviewed_revision": "f" * 40}]:
            with self.subTest(value_type=type(value).__name__):
                self.file.write_text(json.dumps(value))
                with self.assertRaises(RuntimeError):
                    self.check()
        self.file.write_text(" " * 2049)
        with self.assertRaises(RuntimeError):
            self.check()
        self.file.unlink()
        self.file.symlink_to(self.root / "records.json")
        with self.assertRaises(RuntimeError):
            self.check()

    def test_working_tree_reader_denies_dirty_or_old_checkout_and_history_rewrite(self):
        self.save()
        (self.root / "records.json").write_text("dirty")
        with self.assertRaises(RuntimeError):
            self.check(revision=False)
        self.git("add", ".")
        self.git("commit", "-m", "new record")
        # A fresh ref does not grant authority to different checked-out data.
        with self.assertRaises(RuntimeError):
            self.check(revision=False)
        self.git("checkout", "--orphan", "unrelated")
        self.git("commit", "-m", "unrelated root")
        self.revision = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/main", self.revision)
        self.value["record_revision"] = self.revision
        self.save()
        with self.assertRaises(RuntimeError):
            self.check()

    def test_all_existing_canonical_snapshot_readers_require_the_commissioned_checkpoint(self):
        # Minimal native repo proves freshness fails before any schema-dependent
        # record can become authority. Existing full reader suites prove schemas.
        with patch.dict(os.environ, self.env, clear=True):
            for reader, operation, exception in [
                (IntakeAuthority(self.root, None), lambda r: r.snapshot(), IntakeUnavailable),
                (InventoryAuthority(self.root, None), lambda r: r.snapshot(), InventoryUnavailable),
                (PrototypeLandingAuthority(self.root, None), lambda r: r.snapshot("prototype:sample", "record://prototype-captures/sample"), PrototypeLandingUnavailable),
                (PrototypeMaturityAuthority(self.root, None), lambda r: r.snapshot("prototype:sample", []), PrototypeMaturityUnavailable),
                (PrototypeClosureAuthority(self.root), lambda r: r.current_revision(), PrototypeClosureUnavailable),
            ]:
                with self.subTest(reader=type(reader).__name__), self.assertRaisesRegex(exception, "deployment-owned"):
                    operation(reader)
            reader = ProductPublicationAuthority(self.root, {"workspace-governance": self.root}, None)
            with self.assertRaisesRegex(ProductPublicationUnavailable, "deployment-owned"):
                reader.snapshot({"repo": "workspace-governance"})
            repository = RepositoryReadinessService.__new__(RepositoryReadinessService)
            repository._authority_root = self.root
            for read in (repository._read_required, repository._read_optional):
                with self.assertRaisesRegex(RepositoryReadinessUnavailable, "deployment-owned"):
                    read(Path("records.json"))


if __name__ == "__main__":
    unittest.main()
