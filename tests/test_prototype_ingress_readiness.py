from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest import TestCase

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
import yaml


from control_fabric_core.canonical_json import canonical_json_bytes
from control_fabric_core.db import metadata
from control_fabric_core.db.models import LedgerEvent, PrototypeIngressReadinessReceipt
from control_fabric_core.prototype_ingress_contracts import prototype_packet_digest
from control_fabric_core.prototype_ingress_contracts import (
    PrototypeIngressContractBundle,
    PrototypeIngressContractError,
)
from control_fabric_core.prototype_ingress_readiness import (
    PrototypeIngressReadinessContractError,
    PrototypeIngressReadinessService,
)


IMPLEMENTATION_REF = "f" * 40
SERVICE_IDENTITY_REF = (
    "kubernetes://devint-governance-control-fabric/"
    "serviceaccount/workspace-governance-control-fabric-api"
)


class PrototypeSourceFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        self._git("init", "--initial-branch=main")
        self._git("config", "user.name", "Prototype Ingress Test")
        self._git("config", "user.email", "prototype-ingress@example.invalid")
        (root / "README.md").write_text("# Fixture\n", encoding="utf-8")
        self._commit("fixture base")
        self.base_commit = self._git("rev-parse", "HEAD")

        self.prototype_id = "sample-prototype"
        self.baseline_id = "sample-prototype-2026-08-25"
        self.baseline_ref = f"record://design-baselines/{self.baseline_id}"
        self.baseline = {
            "schema_version": 1,
            "prototype_id": self.prototype_id,
            "baseline_id": self.baseline_id,
            "approved_on": "2026-08-25",
            "approved_by": "operator:workspace-owner",
            "decision": "approved",
            "source_refs": ["repo://workspace-prototype-studio@fixture"],
            "evidence_refs": ["record://evidence/baseline-review"],
        }
        self.prototype = {
            "id": self.prototype_id,
            "name": "Sample Prototype",
            "lifecycle": "baseline-approved",
            "owner": "Workspace Prototype Studio",
            "visibility_tier": "private-internal",
            "data_mode": "synthetic",
            "mutation_boundary": "prototype-local",
            "design_baseline_ref": self.baseline_ref,
            "linked_records": [],
        }
        self.registry = {
            "schema_version": 1,
            "studio": {"owner_repo": "workspace-prototype-studio"},
            "prototypes": [self.prototype],
        }
        self._write_yaml(root / "prototypes.yaml", self.registry)
        self._write_yaml(
            root / "records/design-baselines" / f"{self.baseline_id}.yaml",
            self.baseline,
        )
        self._commit("approve baseline")
        self.head_commit = self._git("rev-parse", "HEAD")
        self.packet = self._build_packet()
        self._store_packet(self.packet)

    def _build_packet(self) -> dict:
        content = {
            "intent": "governed-delivery",
            "target": "workspace-delivery-art",
            "source": {
                "kind": "prototype",
                "prototype_id": self.prototype_id,
                "record_ref": f"record://prototypes/{self.prototype_id}",
                "record_version": self.head_commit,
                "lifecycle": "baseline-approved",
                "owner": self.prototype["owner"],
                "repository": "workspace-prototype-studio",
                "revision": {
                    "ref": "refs/heads/main",
                    "base_commit": self.base_commit,
                    "head_commit": self.head_commit,
                    "tree": self._git("rev-parse", f"{self.head_commit}^{{tree}}"),
                },
            },
            "baseline": {
                "record_ref": self.baseline_ref,
                "baseline_id": self.baseline_id,
                "schema_version": self.baseline["schema_version"],
                "version": f"{self.baseline_id}@{prototype_packet_digest(self.baseline)}",
                "record_digest": prototype_packet_digest(self.baseline),
            },
            "work": {
                "title": "Sample governed continuation",
                "objective": "Continue the approved prototype through governed Delivery.",
                "included_scope": ["Preserve the approved operator workflow."],
                "excluded_scope": ["Do not authorize production deployment."],
                "remaining_work": ["Wire the durable backend adapter."],
            },
            "posture": {
                "visibility_tier": self.prototype["visibility_tier"],
                "data_mode": self.prototype["data_mode"],
                "mutation_boundary": self.prototype["mutation_boundary"],
            },
            "custody": {
                "classification": "existing-repo",
                "repository_mode": "existing",
                "repository_gate_state": "resolved",
                "owner": "workspace-prototype-studio",
                "source_ref": "repo://workspace-prototype-studio@main",
                "rationale": "The governed source already has a durable repository.",
            },
            "authorization": {
                "decision": "approved",
                "operator_id": "operator:workspace-owner",
                "decision_ref": "record://prototype-decisions/sample-delivery-handoff",
            },
            "evidence_refs": ["record://evidence/baseline-review"],
            "rationale": "The approved baseline requires governed continuation.",
        }
        digest = prototype_packet_digest(content)
        packet_id = f"{self.prototype_id}-{digest.removeprefix('sha256:')}"
        return {
            "schema_version": 1,
            "packet_id": packet_id,
            "packet_ref": f"record://delivery-packets/{packet_id}",
            "packet_digest": digest,
            "content": content,
        }

    def _store_packet(self, packet: dict) -> None:
        current = copy.deepcopy(self.registry)
        current_prototype = current["prototypes"][0]
        current_prototype["lifecycle"] = "graduating"
        current_prototype["delivery_packet_ref"] = packet["packet_ref"]
        current_prototype["linked_records"].append(
            {
                "role": "delivery-packet",
                "ref": packet["packet_ref"],
                "system": "prototype-studio",
                "level": "record",
                "label": "Prototype Delivery packet",
            },
        )
        self._write_yaml(self.root / "prototypes.yaml", current)
        packet_path = self.root / "records/delivery-packets" / f"{packet['packet_id']}.json"
        packet_path.parent.mkdir(parents=True, exist_ok=True)
        packet_path.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._commit("store Prototype Delivery packet")

    def remove_packet(self) -> None:
        packet_path = self.root / "records/delivery-packets" / f"{self.packet['packet_id']}.json"
        packet_path.unlink()
        self._commit("remove packet")

    def replace_packet(self, packet: dict) -> None:
        old_path = self.root / "records/delivery-packets" / f"{self.packet['packet_id']}.json"
        old_path.unlink()
        digest = prototype_packet_digest(packet["content"])
        packet_id = f"{self.prototype_id}-{digest.removeprefix('sha256:')}"
        packet["packet_digest"] = digest
        packet["packet_id"] = packet_id
        packet["packet_ref"] = f"record://delivery-packets/{packet_id}"
        current = yaml.safe_load((self.root / "prototypes.yaml").read_text(encoding="utf-8"))
        current["prototypes"][0]["delivery_packet_ref"] = packet["packet_ref"]
        current["prototypes"][0]["linked_records"][0]["ref"] = packet["packet_ref"]
        self._write_yaml(self.root / "prototypes.yaml", current)
        path = self.root / "records/delivery-packets" / f"{packet_id}.json"
        path.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._commit("replace packet fixture")
        self.packet = packet

    def install_replacement_for_bound_head(self) -> None:
        baseline_path = self.root / "records/design-baselines" / f"{self.baseline_id}.yaml"
        forged_baseline = copy.deepcopy(self.baseline)
        forged_baseline["decision"] = "denied"
        self._write_yaml(baseline_path, forged_baseline)
        self._git("add", str(baseline_path.relative_to(self.root)))
        forged_tree = self._git("write-tree")
        forged_commit = self._git(
            "commit-tree",
            forged_tree,
            "-p",
            self.head_commit,
            "-m",
            "forged replacement",
        )
        self._git("reset", "--hard", "HEAD")
        self._git("replace", self.head_commit, forged_commit)

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _commit(self, message: str) -> None:
        self._git("add", "-A")
        self._git("commit", "-m", message)

    @staticmethod
    def _write_yaml(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


class PrototypeIngressReadinessTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.source = PrototypeSourceFixture(Path(self.temp.name) / "workspace-prototype-studio")
        self.engine = create_engine("sqlite:///:memory:")
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.service = PrototypeIngressReadinessService(
            session_factory=self.sessions,
            source_repo_root=self.source.root,
            service_identity_ref=SERVICE_IDENTITY_REF,
            implementation_ref=IMPLEMENTATION_REF,
            clock=lambda: datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc),
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp.cleanup()

    def request(self, *, profile_id: str = "dev-integration", packet: dict | None = None) -> bytes:
        return canonical_json_bytes(
            {
                "schema_version": 1,
                "profile_id": profile_id,
                "packet": packet or self.source.packet,
            },
        )

    def test_committed_packet_issues_replay_safe_non_mutating_allow_receipt(self) -> None:
        created = self.service.issue(self.request(), actor="operator-orchestration-service")
        replay = self.service.issue(self.request(), actor="operator-orchestration-service")
        token = created.receipt["receipt_id"].split(":", 1)[1]
        read = self.service.read(token, actor="operator-orchestration-service")

        self.assertEqual("allow", created.receipt["decision"]["outcome"])
        self.assertTrue(created.receipt["decision"]["target_application_allowed"])
        self.assertEqual("none", created.receipt["decision"]["mutation_authority"])
        self.assertEqual(["eligible"], created.receipt["decision"]["reason_codes"])
        self.assertEqual("created", created.resolution)
        self.assertEqual("reused", replay.resolution)
        self.assertEqual("read", read.resolution)
        self.assertEqual(created.receipt, replay.receipt)
        self.assertEqual(created.receipt, read.receipt)
        with self.sessions() as session:
            self.assertEqual(
                1,
                len(session.scalars(select(PrototypeIngressReadinessReceipt)).all()),
            )
            actions = {event.action for event in session.scalars(select(LedgerEvent)).all()}
        self.assertEqual(
            {
                "prototype-ingress.readiness.persisted",
                "prototype-ingress.readiness.reused",
                "prototype-ingress.readiness.read",
            },
            actions,
        )

    def test_unsupported_profile_issues_deny_receipt_without_mutation_claim(self) -> None:
        result = self.service.issue(
            self.request(profile_id="stage"),
            actor="operator-orchestration-service",
        )

        self.assertEqual("deny", result.receipt["decision"]["outcome"])
        self.assertFalse(result.receipt["decision"]["target_application_allowed"])
        self.assertEqual("none", result.receipt["decision"]["mutation_authority"])
        self.assertEqual(
            ["policy-profile-denied"],
            result.receipt["decision"]["reason_codes"],
        )

    def test_removed_committed_packet_issues_bounded_deny(self) -> None:
        self.source.remove_packet()

        result = self.service.issue(self.request(), actor="operator-orchestration-service")

        self.assertEqual("deny", result.receipt["decision"]["outcome"])
        self.assertEqual(
            ["packet-record-missing"],
            result.receipt["decision"]["reason_codes"],
        )

    def test_packet_integrity_substitution_issues_bounded_deny(self) -> None:
        packet = copy.deepcopy(self.source.packet)
        packet["packet_digest"] = f"sha256:{'0' * 64}"

        result = self.service.issue(
            self.request(packet=packet),
            actor="operator-orchestration-service",
        )

        self.assertEqual(
            ["packet-record-mismatch"],
            result.receipt["decision"]["reason_codes"],
        )

    def test_tree_and_self_referential_provenance_fail_closed(self) -> None:
        packet = copy.deepcopy(self.source.packet)
        packet["content"]["source"]["revision"]["tree"] = "0" * 40
        self.assertEqual(("source-tree-mismatch",), self.service._verify_source(packet))

        original = self.service._git_object_exists

        def self_referential(object_ref: str) -> bool:
            return True if ":records/delivery-packets/" in object_ref else original(object_ref)

        self.service._git_object_exists = self_referential  # type: ignore[method-assign]
        self.assertEqual(
            ("source-provenance-self-referential",),
            self.service._verify_source(self.source.packet),
        )

    def test_baseline_substitution_issues_bounded_deny(self) -> None:
        packet = copy.deepcopy(self.source.packet)
        packet["content"]["baseline"]["record_digest"] = f"sha256:{'0' * 64}"
        self.source.replace_packet(packet)

        result = self.service.issue(self.request(), actor="operator-orchestration-service")

        self.assertEqual(
            ["baseline-binding-invalid"],
            result.receipt["decision"]["reason_codes"],
        )

    def test_source_ref_that_no_longer_descends_from_packet_head_is_denied(self) -> None:
        tree = self.source._git("rev-parse", f"{self.source.head_commit}^{{tree}}")
        orphan = self.source._git("commit-tree", tree, "-m", "unrelated source")
        self.source._git("update-ref", "refs/heads/main", orphan)

        self.assertEqual(
            ("source-projection-stale",),
            self.service._verify_source(self.source.packet),
        )

    def test_local_git_replacement_refs_cannot_change_readiness_truth(self) -> None:
        self.source.install_replacement_for_bound_head()

        result = self.service.issue(
            self.request(),
            actor="operator-orchestration-service",
        )

        self.assertEqual("allow", result.receipt["decision"]["outcome"])
        self.assertEqual(["eligible"], result.receipt["decision"]["reason_codes"])

    def test_contract_bundle_rejects_unpinned_schema_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            contract_root = Path(temp_dir) / "prototype-ingress"
            shutil.copytree(
                Path(__file__).resolve().parents[1] / "contracts/prototype-ingress",
                contract_root,
            )
            schema = contract_root / "prototype-delivery-packet.schema.json"
            schema.write_bytes(schema.read_bytes() + b"\n")

            with self.assertRaisesRegex(
                PrototypeIngressContractError,
                "does not match manifest",
            ):
                PrototypeIngressContractBundle.load(contract_root)

    def test_malformed_packet_is_rejected_without_success_projection(self) -> None:
        request = json.loads(self.request())
        request["packet"]["content"]["custody"]["repository_gate_state"] = "pending"

        with self.assertRaises(PrototypeIngressReadinessContractError) as raised:
            self.service.issue(
                canonical_json_bytes(request),
                actor="operator-orchestration-service",
            )

        self.assertEqual("malformed-packet", raised.exception.code)
        with self.sessions() as session:
            self.assertEqual([], session.scalars(select(PrototypeIngressReadinessReceipt)).all())


if __name__ == "__main__":
    import unittest

    unittest.main()
