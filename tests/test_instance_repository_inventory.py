from __future__ import annotations
import copy
from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase, skipUnless
from unittest.mock import Mock

from fastapi.testclient import TestClient
from control_fabric_core.artifact_registry import ArtifactRegistryAuthorizer
from control_fabric_core.governance_history import GovernanceHistoryAuthorizer
from control_fabric_core.instance_records import InstanceRecordConflict, record_digest
from control_fabric_core.instance_repository_inventory import InstanceRepositoryInventoryService, NativeInstanceInventoryPolicy
from wgcf_api.app import create_app
import test_instance_repository_intake as fixtures


class InstanceInventoryAuthorizationTests(TestCase):
    def test_inventory_read_and_write_roles_are_separate(self):
        service = Mock()
        service.prepare.return_value = {"test": "read-preparation"}
        service.resolve.return_value = {"test": "read-original-outcome"}
        service.accept.return_value = {"test": "accepted"}
        client = TestClient(create_app(instance_repository_inventory=service,
            artifact_registry_authorizer=ArtifactRegistryAuthorizer(oos_secret="o" * 32, reconciler_secret="r" * 32),
            governance_history_authorizer=GovernanceHistoryAuthorizer(caller_id="history-reader", caller_secret="h" * 32)))
        base = "/v1/instance-records/repositories/synthetic-project/inventory"
        for caller, secret in (("history-reader", "h" * 32), ("operator-orchestration-service", "o" * 32)):
            headers = {"x-wgcf-caller-id": caller, "x-wgcf-caller-secret": secret}
            self.assertEqual(client.get(base + "/preparation", headers=headers).status_code, 200)
            self.assertEqual(client.post(base + "/readback", json={}, headers=headers).status_code, 200)
        service.accept.assert_not_called()
        for caller, secret, expected in (("history-reader", "h" * 32, 401), ("workspace-governance-control-fabric", "r" * 32, 403), ("", "", 401)):
            headers = {"x-wgcf-caller-id": caller, "x-wgcf-caller-secret": secret}
            self.assertEqual(client.post(base, json={}, headers=headers).status_code, expected)
        headers = {"x-wgcf-caller-id": "operator-orchestration-service", "x-wgcf-caller-secret": "o" * 32}
        self.assertEqual(client.post(base, json={}, headers=headers).status_code, 200)
        service.accept.assert_called_once_with("synthetic-project", {}, actor="operator-orchestration-service")
        self.assertEqual(client.post(base + "?unexpected=true", json={}, headers=headers).status_code, 400)
        self.assertEqual(client.post(base, content='{"a":1,"a":2}', headers=headers).status_code, 400)


@skipUnless(fixtures.HAS_NATIVE_SOURCE, "native Governance inventory policy source required")
class NativeInstanceInventoryTests(TestCase):
    def setUp(self):
        self.fixture = fixtures.NativeInstanceIntakeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = InstanceRepositoryInventoryService(session_factory=self.fixture.sessions, instance_id="installation-a",
            policy=NativeInstanceInventoryPolicy(self.fixture.policy.authority), clock=self.fixture.service.clock)
        payload = self.fixture.payload()
        request, decision = payload["request"], payload["decision"]
        request["requested_classification"] = "admitted"
        request["request_digest"] = record_digest({k:v for k,v in request.items() if k != "request_digest"})
        decision["request_ref"]["digest"] = request["request_digest"]
        decision["outcome"]["classification"] = "admitted"
        decision["decision_digest"] = record_digest({k:v for k,v in decision.items() if k != "decision_digest"})
        proof = {"schema_version": 1, "scope": "source-owner-controls-readiness", "owner_repo": "synthetic-project",
            "provider_repository": request["requested_record"]["provider_repository"], "fetched_base": "d" * 40,
            "canonical_checkout_commit": "e" * 40, "evidence_profile_digest": "sha256:" + "f" * 64,
            "source_request_ref": "oos://repository-owner-bootstrap/requests/synthetic-proof", "source_request_revision": "2026-11-04T00:01:30Z",
            "app_acceptance": False, "inventory_admission": False, "runtime_activation": False}
        proof["receipt_digest"] = record_digest(proof)
        payload["owner_readiness"] = proof
        self.current = self.fixture.accept(payload)

    def payload(self, action="promote", current=None):
        current = current or self.current
        before = current["record"]
        value = {"posture": "active", "lifecycle": "active", "repo_class": "product-source", "requires_security_bindings": False,
            "owns": ["Project source and tests"], "must_not_own": ["Shared runtime or Security policy"], "allowed_authoritative_refs": [],
            "provider_repository": before["provider_repository"], "validation_behavior": before["validation_behavior"]} if action == "promote" else None
        request = {"schema_version": 1, "artifact_type": "instance-repository-inventory-request", "instance_id": "installation-a",
            "request_id": f"inventory:{action}", "requested_at": "2026-11-04T00:02:00Z", "operator_ref": self.fixture.operator,
            "correlation_ref": f"console://inventory/{action}", "idempotency_key": f"inventory:{action}", "action": action,
            "target": {"kind": "repo", "name": "synthetic-project", "record_id": "repo:synthetic-project"},
            "expected_state": {"record_version": current["receipt"]["version"], "record_digest": current["receipt"]["record_digest"]},
            "requested_value": value, "prior_receipt_digest": current["receipt"]["receipt_digest"] if action == "restore" else None,
            "reason": None if action == "promote" else "Operator-approved source lifecycle change",
            "impact_acknowledgements": [] if action == "promote" else ["Current source references follow the requested posture"],
            "approval_refs": ["console://inventory/acceptance"], "operator_acceptance": {"state": "accepted", "operator_ref": self.fixture.operator,
                "recorded_at": "2026-11-04T00:02:00Z"}, "policy_ref": self.service.policy.context().policy_ref}
        request["request_digest"] = record_digest(request)
        return {"schema_version": 1, "request": request, "operator_ref": self.fixture.operator}

    def accept(self, payload):
        return self.service.accept("synthetic-project", payload, actor=self.fixture.actor)

    def test_same_record_promotes_suspends_restores_and_preserves_original_receipt_replay(self):
        original = self.payload()
        promoted = self.accept(original)
        stopped = self.accept(self.payload("suspend", promoted))
        restored = self.accept(self.payload("restore", stopped))
        self.assertEqual(promoted["receipt"]["version"], 2)
        self.assertEqual(stopped["receipt"]["version"], 3)
        self.assertEqual(restored["receipt"]["version"], 4)
        self.assertEqual(restored["record"]["posture"], "active")
        self.assertEqual(self.accept(original), promoted)
        history = self.service._store().history(instance_id="installation-a", record_id="repo:synthetic-project")
        self.assertEqual([row["receipt"]["version"] for row in history["records"]], [4, 3, 2, 1])
        helper = self.fixture.source / "scripts/workspace_instance_inventory.py"
        helper.write_text(helper.read_text() + "\n# Synthetic changed policy\n")
        recovered = self.service.resolve("synthetic-project", original)
        self.assertTrue(recovered["found"])
        self.assertEqual(recovered["receipt"], promoted["receipt"])
        self.assertEqual(recovered["record"], promoted["record"])
        self.assertNotIn("command", recovered)

    def test_same_operation_is_serialized_and_changed_input_conflicts_before_any_new_version(self):
        original = self.payload()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.accept(copy.deepcopy(original)), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["receipt"]["version"], 2)
        wrong = copy.deepcopy(original)
        wrong["request"]["requested_value"]["owns"] = ["Changed after acceptance"]
        wrong["request"]["request_digest"] = record_digest({k:v for k,v in wrong["request"].items() if k != "request_digest"})
        with self.assertRaises(InstanceRecordConflict): self.accept(wrong)
        self.assertEqual(self.service._store().read(instance_id="installation-a", record_id="repo:synthetic-project")["receipt"]["version"], 2)

    def test_original_readback_absence_does_not_promote_or_accept_any_record(self):
        original = self.payload()
        self.assertFalse(self.service.resolve("synthetic-project", original)["found"])
        current = self.service._store().read(instance_id="installation-a", record_id="repo:synthetic-project")
        self.assertEqual(current, self.current)
