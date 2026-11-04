import copy
import json
from unittest import TestCase, skipUnless

from control_fabric_core.repository_readiness import RepositoryReadinessService, RepositoryReadinessUnavailable
from test_repository_readiness import IMPLEMENTATION_REF, SERVICE_IDENTITY_REF
import test_instance_repository_inventory as inventory_fixture
import test_instance_repository_intake as intake_fixture


@skipUnless(intake_fixture.HAS_NATIVE_SOURCE, "native Governance policy source required")
class InstanceRepositoryReadinessTests(TestCase):
    def setUp(self):
        self.fixture = inventory_fixture.NativeInstanceInventoryTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.promoted = self.fixture.accept(self.fixture.payload())
        intake = self.fixture.fixture.service
        self.service = RepositoryReadinessService(session_factory=self.fixture.fixture.sessions,
            authority_repo_root=self.fixture.fixture.source, service_identity_ref=SERVICE_IDENTITY_REF, implementation_ref=IMPLEMENTATION_REF,
            instance_authority_reader=lambda name: intake.source_authority(name, "ordinary-source"))
        proof = intake.source_authority("synthetic-project", "ordinary-source")["source_authority"]
        self.reference = {key: proof[key] for key in ("canonical_authority", "record_ref", "policy_ref")}
        self.request = {"schema_version": 1, "profile_id": "dev-integration", "policy_scope": "delivery-catalog-owner-repo",
            "repo_name": "synthetic-project", "repo_ref": "repo://synthetic-project", "expected_owner_repo": "synthetic-project",
            "catalog_value_key": "synthetic-project", "expected_authority_digest": self.promoted["receipt"]["record_digest"], "expected_instance_record": self.reference}

    def test_actual_internal_inventory_issues_a_durable_ready_reference_without_a_public_repo_or_rule_entry(self):
        original = self.service.issue(json.dumps(self.request).encode(), actor="operator-orchestration-service")
        self.assertEqual(original.receipt["decision"]["outcome"], "ready")
        self.assertEqual(original.receipt["authority"]["record_ref"], self.reference)
        self.assertEqual(original.receipt["authority"]["record_digest"], self.promoted["receipt"]["record_digest"])
        self.assertIn("instance-operational-records.yaml#source-authority", original.receipt["authority"]["rule_ref"])
        self.assertFalse((self.fixture.fixture.source / "contracts/repo-rules/synthetic-project.yaml").exists())
        again = self.service.issue(json.dumps(self.request).encode(), actor="operator-orchestration-service")
        self.assertEqual(again.receipt, original.receipt)
        token = original.receipt["receipt_id"].split(":", 1)[1]
        self.assertEqual(self.service.read(token, actor="operator-orchestration-service").receipt, original.receipt)

    def test_foreign_namespace_and_stale_version_never_become_ready_and_suspension_invalidates_fresh_linking(self):
        original = self.service.issue(json.dumps(self.request).encode(), actor="operator-orchestration-service")
        for mutate in (lambda value: value["expected_instance_record"]["canonical_authority"].update(instance_id="another-installation"),
                       lambda value: value["expected_instance_record"]["record_ref"].update(version=99)):
            request = copy.deepcopy(self.request); mutate(request)
            result = self.service.issue(json.dumps(request).encode(), actor="operator-orchestration-service")
            self.assertNotEqual(result.receipt["decision"]["outcome"], "ready"); self.assertIsNone(result.reference)
        self.fixture.accept(self.fixture.payload("suspend", self.promoted))
        token = original.receipt["receipt_id"].split(":", 1)[1]
        self.assertEqual(self.service.read(token, actor="operator-orchestration-service").receipt, original.receipt)
        with self.assertRaises(RepositoryReadinessUnavailable): self.service.issue(json.dumps(self.request).encode(), actor="operator-orchestration-service")


if __name__ == "__main__":
    import unittest
    unittest.main()
