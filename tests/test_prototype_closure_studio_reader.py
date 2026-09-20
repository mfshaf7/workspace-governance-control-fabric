from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

import yaml

from control_fabric_core.prototype_closure_authority import (
    ClosureSource, PrototypeClosureAuthority, PrototypeClosureUnavailable, studio_digest,
)
from control_fabric_core.prototype_closure_evidence import ClosureEvidenceLookup
from control_fabric_core.prototype_closure_studio_reader import StudioClosureOwnerReader


class StudioClosureOwnerReaderTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Closure Test")
        self.git("config", "user.email", "closure@example.invalid")
        source = self.repo / "prototypes/sample-tool/README.md"
        source.parent.mkdir(parents=True)
        source.write_text("retained source\n", encoding="utf-8")
        self.record = {
            "id": "sample-tool", "lifecycle": "exploring", "source_custody": "incubation-repo",
            "paths": {"readme": "prototypes/sample-tool/README.md"},
        }
        (self.repo / "prototypes.yaml").write_text(
            yaml.safe_dump({"prototypes": [self.record]}), encoding="utf-8",
        )
        schema = {
            "type": "object", "required": [
                "prototype_id", "basis_revision", "basis_lifecycle", "basis_source_custody",
                "source_tree_path", "source_tree_oid", "retained_files", "operator_id",
                "retirement_reason",
            ],
        }
        schema_path = self.repo / "schemas/prototype-closure-retention-plan.schema.json"
        schema_path.parent.mkdir(parents=True)
        schema_bytes = json.dumps(schema).encode()
        schema_path.write_bytes(schema_bytes)
        self.schema_digest = hashlib.sha256(schema_bytes).hexdigest()
        self.commit()
        basis = self.git("rev-parse", "HEAD")
        self.plan = {
            "prototype_id": "sample-tool", "basis_revision": basis,
            "basis_lifecycle": "exploring", "basis_source_custody": "incubation-repo",
            "source_tree_path": "prototypes/sample-tool",
            "source_tree_oid": self.git("rev-parse", f"{basis}:prototypes/sample-tool"),
            "retained_files": [{
                "path": "prototypes/sample-tool/README.md",
                "oid": self.git("rev-parse", f"{basis}:prototypes/sample-tool/README.md"),
            }],
            "operator_id": "agent-gary", "retirement_reason": "No longer pursued",
        }
        digest = studio_digest(self.plan).split(":", 1)[1]
        self.plan_ref = f"record://prototype-closure/sample-tool/retention-plans/{digest}"
        plan_path = self.repo / f"records/prototype-closure/sample-tool/retention-plans/{digest}.json"
        plan_path.parent.mkdir(parents=True)
        plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        self.commit()
        self.authority = PrototypeClosureAuthority(self.repo, trusted_ref="refs/heads/main")
        self.reader = StudioClosureOwnerReader(self.authority)
        self.manifest_patch = patch(
            "control_fabric_core.prototype_closure_studio_reader.load_bundle_manifest",
            return_value={"source_authority": {"retention_plan_schema_sha256": self.schema_digest}},
        )
        self.manifest_patch.start()
        self.addCleanup(self.manifest_patch.stop)

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True,
        ).stdout.strip()

    def commit(self) -> None:
        self.git("add", ".")
        self.git("commit", "-m", "Studio source proof")

    def lookup(self, field: str, requested_ref: str | None) -> ClosureEvidenceLookup:
        return ClosureEvidenceLookup(
            field=field, owner_ref="workspace-prototype-studio", prototype_id="sample-tool",
            requested_ref=requested_ref, subject_ref=None,
            source_revision=self.git("rev-parse", "HEAD"), source_packet_ref=None,
            target_delivery_ref=None, accepted_delivery_target_receipt_ref=None,
            operator_id="agent-gary", retirement_reason="No longer pursued",
        )

    def snapshot(self, event: dict | None = None) -> ClosureSource:
        revision = self.git("rev-parse", "HEAD")
        return ClosureSource(
            revision, self.record, studio_digest(self.record), self.record["lifecycle"],
            "incubation-repo", None, event,
        )

    def test_committed_retention_plan_is_bound_to_decision_and_objects(self) -> None:
        with patch.object(self.authority, "snapshot", return_value=self.snapshot()):
            proof = self.reader.read(self.lookup("retention_plan_ref", self.plan_ref))
            self.assertEqual(proof.ref, self.plan_ref)
            wrong = replace(self.lookup("retention_plan_ref", self.plan_ref),
                            retirement_reason="Different reason")
            with self.assertRaisesRegex(PrototypeClosureUnavailable, "decision differs"):
                self.reader.read(wrong)

    def test_changed_committed_source_invalidates_plan(self) -> None:
        (self.repo / "prototypes/sample-tool/README.md").write_text("changed\n", encoding="utf-8")
        self.commit()
        with patch.object(self.authority, "snapshot", return_value=self.snapshot()):
            with self.assertRaisesRegex(PrototypeClosureUnavailable, "retained files differ"):
                self.reader.read(self.lookup("retention_plan_ref", self.plan_ref))

    def test_retired_source_readback_requires_history_and_plan(self) -> None:
        event_id = "prototype-closure:sample-tool:0001"
        retirement_ref = f"record://prototype-closure/sample-tool/history/{event_id}"
        self.record.update(
            lifecycle="retired", retirement_ref=retirement_ref, closure_event_ref=retirement_ref,
        )
        (self.repo / "prototypes.yaml").write_text(
            yaml.safe_dump({"prototypes": [self.record]}), encoding="utf-8",
        )
        self.commit()
        event = {"event_type": "incubation-retired", "event_id": event_id,
                 "retention_plan_ref": self.plan_ref}
        with patch.object(self.authority, "snapshot", return_value=self.snapshot(event)):
            proof = self.reader.read(self.lookup("retained_source_readback_ref", None))
            self.assertEqual(proof.subject_ref, retirement_ref)
        with patch.object(self.authority, "snapshot", return_value=self.snapshot(None)):
            with self.assertRaisesRegex(PrototypeClosureUnavailable, "history"):
                self.reader.read(self.lookup("retained_source_readback_ref", None))
