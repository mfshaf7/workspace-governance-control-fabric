from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from control_fabric_core.artifact_registry import ArtifactRegistryAuthorizer, ArtifactRegistryForbidden, ArtifactRegistryUnavailable
from control_fabric_core.db.models import metadata
from control_fabric_core.governance_history import GovernanceHistoryAuthorizer
from control_fabric_core.instance_records import InstanceRecordConflict, InstanceRecordStore, record_digest
from control_fabric_core.instance_repository_intake import InstanceRepositoryIntakeService, NativeInstancePolicy
from control_fabric_core.workspace_intake_contracts import IntakeAuthority, IntakeContracts
from wgcf_api.app import create_app


GOVERNANCE_SOURCE = Path(os.environ.get("WGCF_INSTANCE_POLICY_TEST_ROOT", "../workspace-governance")).resolve()
HAS_NATIVE_SOURCE = (GOVERNANCE_SOURCE / "scripts/workspace_instance_records.py").is_file()


class InstanceAcceptanceAuthorizationTests(TestCase):
    def test_platform_source_reader_has_only_fresh_source_eligibility_access(self):
        service, reads = Mock(), Mock()
        service.source_authority.return_value = {"test": "source-eligibility-only"}
        service.source_authority_for_provider.return_value = {"test": "exact-physical-source-only"}
        auth = ArtifactRegistryAuthorizer(oos_secret="o" * 32, reconciler_secret="r" * 32,
            instance_source_reader_secret="p" * 32)
        caller = "platform-engineering-source-reader"
        headers = {"x-wgcf-caller-id": caller, "x-wgcf-caller-secret": "p" * 32}
        client = TestClient(create_app(instance_repository_intake=service, instance_record_reads=reads,
            artifact_registry_authorizer=auth,
            governance_history_authorizer=GovernanceHistoryAuthorizer(caller_id="history-reader", caller_secret="h" * 32)))
        base = "/v1/instance-records/repositories"
        for operation in ("bootstrap", "configure-verification", "ordinary-source"):
            self.assertEqual(client.get(base + "/synthetic-project/source-authority", params={"operation": operation}, headers=headers).status_code, 200)
        self.assertEqual(service.source_authority.call_count, 3)
        physical = "/v1/instance-records/provider-repositories/github/123/source-authority?operation=ordinary-source"
        self.assertEqual(client.get(physical, headers=headers).status_code, 200)
        service.source_authority_for_provider.assert_called_once_with("123", "ordinary-source")
        self.assertEqual(client.get(physical + "&operation=bootstrap", headers=headers).status_code, 400)
        self.assertEqual(client.get(physical).status_code, 401)
        for suffix in ("", "/synthetic-project", "/synthetic-project/history", "/synthetic-project/intake/preparation", "/synthetic-project/inventory/preparation"):
            self.assertEqual(client.get(base + suffix, headers=headers).status_code, 403)
        for suffix in ("/intake", "/inventory", "/intake/readback", "/inventory/readback"):
            self.assertEqual(client.post(base + "/synthetic-project" + suffix, json={}, headers=headers).status_code, 403)
        for operation in ("register", "read", "evaluate-readiness", "reconcile", "accept-instance-decision", "read-instance-records"):
            with self.assertRaises(ArtifactRegistryForbidden): auth.authorize(caller, "p" * 32, operation)
        service.accept.assert_not_called()
        service.prepare.assert_not_called()
        reads.list.assert_not_called()
        self.assertEqual(client.get("/v1/instance-records/repository-status?name=synthetic-project", headers=headers).status_code, 403)
        reads.lookup.assert_not_called()

    def test_optional_platform_source_reader_binding_fails_closed_when_partial_or_reused(self):
        from control_fabric_core.artifact_registry import OOS_CALLER_SECRET_ENV, RECONCILER_CALLER_SECRET_ENV
        base = {OOS_CALLER_SECRET_ENV: "o" * 32, RECONCILER_CALLER_SECRET_ENV: "r" * 32}
        for extra in ({"WGCF_INSTANCE_SOURCE_CALLER_ID": "platform-engineering-source-reader"},
                      {"WGCF_INSTANCE_SOURCE_CALLER_SECRET": "p" * 32},
                      {"WGCF_INSTANCE_SOURCE_CALLER_ID": "operator-orchestration-service", "WGCF_INSTANCE_SOURCE_CALLER_SECRET": "p" * 32},
                      {"WGCF_INSTANCE_SOURCE_CALLER_ID": "platform-engineering-source-reader", "WGCF_INSTANCE_SOURCE_CALLER_SECRET": "o" * 32}):
            with patch.dict(os.environ, {**base, **extra}, clear=True), self.assertRaises(ArtifactRegistryUnavailable):
                ArtifactRegistryAuthorizer.from_environment()

    def test_only_workflow_service_can_accept_instance_decisions(self):
        service = Mock()
        service.accept.return_value = {"test": "authorization-boundary-only"}
        authorizer = ArtifactRegistryAuthorizer(oos_secret="o" * 32, reconciler_secret="r" * 32)
        history = GovernanceHistoryAuthorizer(caller_id="history-reader", caller_secret="h" * 32)
        client = TestClient(create_app(instance_repository_intake=service,
                                      artifact_registry_authorizer=authorizer, governance_history_authorizer=history))
        route = "/v1/instance-records/repositories/synthetic-project/intake"
        for caller, secret, expected in (("", "", 401), ("history-reader", "h" * 32, 401),
                                         ("workspace-governance-control-fabric", "r" * 32, 403)):
            response = client.post(route, json={}, headers={"x-wgcf-caller-id": caller, "x-wgcf-caller-secret": secret})
            self.assertEqual(response.status_code, expected)
        service.accept.assert_not_called()
        response = client.post(route, json={}, headers={"x-wgcf-caller-id": "operator-orchestration-service", "x-wgcf-caller-secret": "o" * 32})
        self.assertEqual(response.status_code, 200)
        service.accept.assert_called_once_with("synthetic-project", {}, actor="operator-orchestration-service")


    def test_readers_can_prepare_and_recover_but_only_oos_can_write(self):
        service = Mock()
        service.prepare.return_value = {"test": "read-only-preparation"}
        service.resolve.return_value = {"test": "read-only-original-outcome"}
        reads = Mock()
        reads.list.return_value = {"test": "read-only-list"}
        client = TestClient(create_app(instance_repository_intake=service, instance_record_reads=reads,
            artifact_registry_authorizer=ArtifactRegistryAuthorizer(oos_secret="o" * 32, reconciler_secret="r" * 32),
            governance_history_authorizer=GovernanceHistoryAuthorizer(caller_id="history-reader", caller_secret="h" * 32)))
        base = "/v1/instance-records/repositories"
        for caller, secret in (("history-reader", "h" * 32), ("operator-orchestration-service", "o" * 32)):
            headers = {"x-wgcf-caller-id": caller, "x-wgcf-caller-secret": secret}
            self.assertEqual(client.get(base, headers=headers).status_code, 200)
            self.assertEqual(client.get(base + "/synthetic-project/intake/preparation", headers=headers).status_code, 200)
            self.assertEqual(client.post(base + "/synthetic-project/intake/readback", json={}, headers=headers).status_code, 200)
        service.accept.assert_not_called()
        headers = {"x-wgcf-caller-id": "workspace-governance-control-fabric", "x-wgcf-caller-secret": "r" * 32}
        self.assertEqual(client.get(base, headers=headers).status_code, 403)
        self.assertEqual(client.get(base + "/synthetic-project/intake/preparation", headers=headers).status_code, 403)
        self.assertEqual(client.post(base + "/synthetic-project/intake/readback", json={}, headers=headers).status_code, 403)
        for method, suffix in (("get", "/synthetic-project/intake/preparation"), ("post", "/synthetic-project/intake/readback")):
            self.assertEqual(getattr(client, method)(base + suffix).status_code, 401)


@skipUnless(HAS_NATIVE_SOURCE, "native Governance policy source required; set WGCF_INSTANCE_POLICY_TEST_ROOT for composed proof")
class NativeInstanceIntakeTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / "workspace-governance"
        self.source.mkdir()
        for directory in ("scripts", "contracts"):
            shutil.copytree(GOVERNANCE_SOURCE / directory, self.source / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (self.source / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        self.git("init", "-b", "main")
        self.git("-c", "user.name=Native policy fixture", "-c", "user.email=fixture@example.invalid", "add", ".")
        self.git("-c", "user.name=Native policy fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Synthetic native policy fixture")
        revision = self.git("rev-parse", "HEAD")
        contracts = IntakeContracts.load()
        contracts = replace(contracts, manifest={**contracts.manifest, "authority_commit": revision})
        self.policy = NativeInstancePolicy(IntakeAuthority(self.source, contracts, trusted_ref="refs/heads/main"))
        self.engine = create_engine(f"sqlite:///{root / 'private.sqlite'}")
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.service = InstanceRepositoryIntakeService(session_factory=self.sessions, instance_id="installation-a", policy=self.policy,
                                                       clock=lambda: datetime(2026, 11, 4, 0, 2, tzinfo=timezone.utc))
        self.operator = "operator:test"
        self.actor = "operator-orchestration-service"

    def git(self, *args):
        process = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.source), *args],
                                 capture_output=True, text=True, timeout=20,
                                 env={"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"})
        if process.returncode:
            self.fail(process.stderr)
        return process.stdout.strip()

    def payload(self, *, current=None, notes="Synthetic private operational metadata", provider_name="example-org/synthetic-project"):
        request = {"schema_version": 1, "artifact_type": "instance-repository-intake-request", "instance_id": "installation-a", "custody_request_id": "synthetic",
                   "request_id": "synthetic-request:" + ("update" if current else "add"), "requested_at": "2026-11-04T00:00:00Z",
                   "requester_ref": self.operator, "target": {"kind": "repo", "name": "synthetic-project", "record_id": "repo:synthetic-project"},
                   "action": "update" if current else "add", "requested_classification": "proposed", "owner_route": "synthetic-project",
                   "source": {"class": "repository-custody", "ref": "oos://repository-custody/requests/synthetic/intake/" + "a" * 64, "digest": "sha256:" + "b" * 64},
                   "requested_record": {"kind": "repo", "repo_class": "product-source", "requires_security_bindings": False, "security_owner": None,
                                        "notes": notes, "provider_repository": {"provider": "github", "provider_repository_id": "123", "full_name": provider_name},
                                        "validation_behavior": {"posture": "covered-by-owner-repo", "wgcf_graph_role": "product-runtime-source", "catalog_refs": [], "notes": "Owner source controls only."}},
                   "expected_state": {"record_version": current["receipt"]["version"] if current else None, "record_digest": current["receipt"]["record_digest"] if current else None},
                   "policy_ref": self.policy.context().policy_ref,
                   "idempotency_key": "synthetic-update" if current else "synthetic-add"}
        request["request_digest"] = record_digest(request)
        decision = {"schema_version": 2, "artifact_type": "workspace-intake-decision", "decision_id": "decision:" + request["request_id"],
                    "decided_at": "2026-11-04T00:01:00Z", "request_ref": {"id": request["request_id"], "digest": request["request_digest"]},
                    "target": copy.deepcopy(request["target"]), "decision_source": "operator",
                    "operator_acceptance": {"state": "accepted", "operator_ref": self.operator, "recorded_at": "2026-11-04T00:01:00Z"},
                    "outcome": {"status": "allowed", "classification": "proposed", "owner_route": "synthetic-project", "approved_record": copy.deepcopy(request["requested_record"]), "findings": []}}
        decision["decision_digest"] = record_digest(decision)
        attestation = {"attested_by": "operator-orchestration-service", "source": copy.deepcopy(request["source"]),
                       "provider_repository": copy.deepcopy(request["requested_record"]["provider_repository"]),
                       "evidence_refs": ["oos://repository-custody/receipts/synthetic"]}
        attestation["attestation_digest"] = record_digest(attestation)
        return {"schema_version": 1, "request": request, "decision": decision, "operator_ref": self.operator,
                "source_attestation": attestation, "owner_readiness": None}

    def accept(self, payload):
        return self.service.accept("synthetic-project", payload, actor=self.actor)

    def test_native_policy_through_transactional_readback_and_original_request_recovery(self):
        payload = self.payload()
        original = self.accept(payload)
        self.assertEqual(original["receipt"]["version"], 1)
        update = self.accept(self.payload(current=original, notes="Updated reviewed purpose"))
        self.assertEqual(update["receipt"]["version"], 2)
        self.assertEqual(self.accept(payload), original)
        retained = self.service._store().operation(instance_id="installation-a", idempotency_key="synthetic-add", actor=self.actor)
        self.assertEqual(retained["command"]["decision"]["readiness"]["outcome"], "allowed")
        self.assertEqual(record_digest(retained["command"]["decision"]["readiness"]), original["receipt"]["readiness_digest"])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_preparation_proves_legacy_authority_or_exact_internal_state(self):
        legacy = self.service.prepare("workspace-governance")
        self.assertEqual(legacy["canonical_authority"]["kind"], "legacy-git")
        prepared = self.service.prepare("synthetic-project")
        self.assertEqual(prepared["canonical_authority"]["kind"], "internal-instance-record")
        self.assertIsNone(prepared["expected_state"]["record_version"])
        result = self.accept(self.payload())
        self.assertEqual(self.service.prepare("synthetic-project")["expected_state"]["record_digest"], result["receipt"]["record_digest"])

    def test_altered_accepted_input_cannot_reuse_committed_identity(self):
        payload = self.payload()
        self.accept(payload)
        changed = self.payload(notes="Different approval")
        with self.assertRaises(InstanceRecordConflict):
            self.accept(changed)

    def test_concurrent_identical_calls_return_one_original_native_result(self):
        payload = self.payload()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.accept(copy.deepcopy(payload)), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["receipt"]["version"], 1)
        self.assertEqual(len(self.service._store().history(instance_id="installation-a", record_id="repo:synthetic-project")["records"]), 1)

    def test_dirty_policy_cannot_create_records_but_committed_replay_survives_later_policy_change(self):
        payload = self.payload()
        original = self.accept(payload)
        helper = self.source / "scripts/workspace_instance_records.py"
        helper.write_text(helper.read_text() + "\n# Synthetic dirty-source test\n")
        self.assertEqual(self.accept(payload), original)
        from control_fabric_core.instance_records import InstanceRecordUnavailable
        with self.assertRaises(InstanceRecordUnavailable):
            self.service.prepare("synthetic-project")

    def test_original_readback_is_read_only_and_recovers_before_changed_policy(self):
        payload = self.payload()
        original_input = {key: payload[key] for key in ("schema_version", "request", "decision", "operator_ref")}
        absent = self.service.resolve("synthetic-project", original_input)
        self.assertFalse(absent["found"])
        self.assertIsNone(self.service._store().read(instance_id="installation-a", record_id="repo:synthetic-project"))
        original = self.accept(payload)
        helper = self.source / "scripts/workspace_instance_records.py"
        helper.write_text(helper.read_text() + "\n# Synthetic unavailable current policy\n")
        readback = self.service.resolve("synthetic-project", original_input)
        self.assertTrue(readback["found"])
        self.assertEqual(readback["record"], original["record"])
        self.assertEqual(readback["receipt"], original["receipt"])
        self.assertNotIn("command", readback)
        changed = copy.deepcopy(original_input)
        changed["operator_ref"] = "another-operator"
        with self.assertRaises(InstanceRecordConflict):
            self.service.resolve("synthetic-project", changed)

    def test_source_authority_reuses_native_policy_and_never_fabricates_active_inventory(self):
        self.accept(self.payload())
        source = self.service.source_authority("synthetic-project", "bootstrap")
        self.assertEqual(source["canonical_authority"]["kind"], "internal-instance-record")
        proof = source["source_authority"]
        self.assertEqual(proof["classification"], "proposed")
        self.assertEqual(proof["intake_entry_ref"]["digest"], proof["record_ref"]["digest"])
        self.assertTrue(proof["source_bootstrap_eligible"])
        self.assertFalse(proof["ordinary_source_eligible"])
        self.assertNotIn("intake_register_digest", proof)
        for operation in ("configure-verification", "ordinary-source"):
            with self.assertRaises(InstanceRecordConflict):
                self.service.source_authority("synthetic-project", operation)
        legacy = self.service.source_authority("workspace-governance", "bootstrap")
        self.assertEqual(legacy["canonical_authority"]["kind"], "legacy-git")
        with self.assertRaises(InstanceRecordConflict):
            self.service.source_authority("missing-project", "bootstrap")

    def test_source_authority_resolves_provider_identity_to_its_actual_alias_without_listing_or_borrowing_ordinary_access(self):
        self.accept(self.payload(provider_name="example-org/different-physical-name"))
        result = self.service.source_authority_for_provider("123", "bootstrap")
        self.assertEqual(result["canonical_authority"]["record_id"], "repo:synthetic-project")
        self.assertEqual(result["source_authority"]["provider_repository"]["full_name"], "example-org/different-physical-name")
        for identifier, operation in (("999", "bootstrap"), ("123", "ordinary-source")):
            with self.assertRaises(InstanceRecordConflict): self.service.source_authority_for_provider(identifier, operation)
