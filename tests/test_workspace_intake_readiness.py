from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
import yaml

from control_fabric_core.artifact_registry import ArtifactRegistryAuthorizer
from control_fabric_core.db.models import LedgerEvent, WorkspaceIntakeEvaluationRecord, metadata
from control_fabric_core.workspace_intake_contracts import (
    IntakeAuthority, IntakeContracts, IntakeUnavailable, artifact_digest, canonical_bytes, digest,
)
from control_fabric_core.workspace_intake_readiness import (
    IntakeConflict, IntakeRequestError, WorkspaceIntakeReadinessService,
    build_workspace_intake_readiness_runtime,
)
from wgcf_api.app import create_app


class WorkspaceIntakeReadinessTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "workspace-governance"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Intake Test")
        self.git("config", "user.email", "intake@example.invalid")
        contracts = IntakeContracts.load()
        for path, raw in contracts.files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        self.register = {"schema_version": 2, "repos": {}, "products": {}, "components": {}}
        self.write("intake-register", self.register)
        self.write("repos", {"repos": {"workspace-governance": {}}, "retired_repos": {}})
        self.write("products", {"products": {}})
        self.write("components", {"components": {}})
        self.write("governed-intake-assist", {"governed_intake_assist": {}})
        self.commit()
        self.pin = self.git("rev-parse", "HEAD")
        self.contracts = replace(contracts, manifest={**contracts.manifest, "authority_commit": self.pin})
        self.authority = IntakeAuthority(self.repo, self.contracts, trusted_ref="refs/heads/main")
        self.engine = create_engine(f"sqlite:///{self.root / 'ledger.sqlite'}")
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.service = self.new_service()
        self.client = TestClient(create_app(
            workspace_intake_readiness=self.service,
            artifact_registry_authorizer=ArtifactRegistryAuthorizer(
                oos_secret="o" * 32, reconciler_secret="r" * 32,
            ),
        ))
        self.addCleanup(self.client.close)
        self.headers = {
            "x-wgcf-caller-id": "operator-orchestration-service",
            "x-wgcf-caller-secret": "o" * 32,
        }

    def new_service(self) -> WorkspaceIntakeReadinessService:
        return WorkspaceIntakeReadinessService(
            session_factory=self.sessions, authority=self.authority,
            service_identity_ref="spiffe://test/wgcf",
            implementation_ref="1" * 40,
            clock=lambda: datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc),
        )

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True,
        ).stdout.strip()

    def write(self, name: str, data: dict) -> None:
        (self.repo / "contracts" / f"{name}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    def commit(self) -> None:
        self.git("add", ".")
        self.git("commit", "-m", "Test committed authority")

    def envelope(self, kind: str = "component", classification: str = "proposed") -> dict:
        record = {
            "repo": {
                "kind": "repo", "repo_class": "product", "requires_security_bindings": True,
                "security_owner": "security-architecture", "notes": "Test repository.",
            },
            "product": {
                "kind": "product", "platform_owner": "platform-engineering",
                "security_owner": "security-architecture", "runtime_owner": "runtime-owner",
                "source_owners": ["source-owner"], "intended_endpoint": "loopback", "notes": "Test product.",
            },
            "component": {
                "kind": "component", "component_class": "shared-platform",
                "owner_repo": "platform-engineering", "security_owner": "security-architecture",
                "product": None, "notes": "Test component.",
            },
        }[kind]
        if classification == "out-of-scope":
            record = {
                key: value if key in {"kind", "notes"} else ([] if key == "source_owners" else None)
                for key, value in record.items()
            }
        else:
            record["validation_behavior"] = {
                "posture": "proposed-profile-gated",
                "wgcf_graph_role": "proposed-shared-platform-component",
                "catalog_refs": [], "notes": "No runtime activation.",
            }
        target = {"kind": kind, "name": "example", "record_id": f"{kind}:example"}
        request = {
            "schema_version": 2, "artifact_type": "workspace-intake-request",
            "request_id": f"request:{kind}:{classification}", "requested_at": "2026-09-05T10:00:00Z",
            "requester_ref": "operator:test",
            "source": {"class": "direct", "ref": "proposal:test", "digest": digest("source")},
            "target": target, "action": "add", "requested_classification": classification,
            "owner_route": "platform-engineering", "requested_record": record,
            "expected_state": {
                "register_digest": digest(self.register), "record_version": None, "record_digest": None,
            },
            "idempotency_key": f"mutation:{kind}:{classification}",
        }
        decision = {
            "schema_version": 2, "artifact_type": "workspace-intake-decision",
            "decision_id": f"decision:{kind}:{classification}", "decided_at": "2026-09-05T10:01:00Z",
            "target": copy.deepcopy(target), "decision_source": "operator",
            "operator_acceptance": {
                "state": "accepted", "operator_ref": "operator:test", "recorded_at": "2026-09-05T10:01:00Z",
            },
            "outcome": {
                "status": "allowed", "classification": classification, "owner_route": request["owner_route"],
                "approved_record": copy.deepcopy(record), "findings": [],
            },
        }
        value = {
            "schema_version": 1, "evaluation_id": f"evaluation:{kind}:{classification}",
            "session_ref": "session:test", "execution_ref": "execution:test",
            "authority_revision": self.git("rev-parse", "HEAD"),
            "request": request, "decision": decision,
        }
        return self.bind(value)

    @staticmethod
    def bind(value: dict) -> dict:
        request, decision = value["request"], value["decision"]
        request["request_digest"] = artifact_digest(request, "request_digest")
        decision["request_ref"] = {"id": request["request_id"], "digest": request["request_digest"]}
        decision["decision_digest"] = artifact_digest(decision, "decision_digest")
        value["evaluation_digest"] = artifact_digest(value, "evaluation_digest")
        return value

    def post(self, value: dict):
        return self.client.post("/v1/readiness/workspace-intake", content=canonical_bytes(value), headers=self.headers)

    def test_positive_api_matrix_receipt_read_restart_replay_without_mutation(self) -> None:
        before = self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")
        repo = self.root / "example"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        for name in ("README.md", "AGENTS.md"):
            (repo / name).write_text("Test owner file.", encoding="utf-8")
        for kind in ("repo", "product", "component"):
            for classification in ("out-of-scope", "proposed", "admitted"):
                with self.subTest(kind=kind, classification=classification):
                    value = self.envelope(kind, classification)
                    response = self.post(value)
                    self.assertEqual(response.status_code, 200, response.text)
                    result = response.json()
                    receipt = result["receipt"]
                    self.assertEqual(receipt["outcome"], "allowed", receipt["findings"])
                    self.assertFalse(receipt["canonical_mutation"])
                    self.assertEqual(receipt["authority"]["revision"], self.pin)
                    self.assertEqual(receipt["receipt_digest"], artifact_digest(receipt, "receipt_digest"))
                    retry = self.new_service().issue(canonical_bytes(value), actor="operator-orchestration-service")
                    self.assertEqual(retry["receipt"], receipt)
                    self.assertEqual(retry["ledger"]["resolution"], "reused")
                    token = receipt["receipt_digest"].removeprefix("sha256:")
                    read = self.client.get(f"/v1/readiness/workspace-intake/{token}", headers=self.headers)
                    self.assertEqual(read.json()["receipt"], receipt)
        self.assertEqual((self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")), before)
        with self.sessions() as session:
            self.assertEqual(len(session.scalars(select(WorkspaceIntakeEvaluationRecord)).all()), 9)
            self.assertEqual(len(session.scalars(select(LedgerEvent)).all()), 27)

    def test_negative_readiness_matrix_preserves_denial_receipts(self) -> None:
        cases = {
            "owner-route-missing": lambda v: v["request"].update(owner_route=" "),
            "component-ownership-missing": lambda v: v["request"]["requested_record"].update(owner_repo=None),
            "validation-behavior-missing": lambda v: v["request"]["requested_record"].pop("validation_behavior"),
            "stale-register": lambda v: v["request"]["expected_state"].update(register_digest=digest("stale")),
            "stale-authority": lambda v: v.update(authority_revision="0" * 40),
            "target-mismatch": lambda v: v["request"]["target"].update(record_id="component:wrong"),
            "record-not-found": lambda v: v["request"].update(action="update"),
            "decision-chronology-invalid": lambda v: v["decision"].update(decided_at="2026-09-04T10:00:00Z"),
            "future-decision": lambda v: v["decision"].update(decided_at="2026-09-06T10:00:00Z"),
            "operator-decision-not-allowed": lambda v: v["decision"]["outcome"].update(status="denied"),
        }
        for code, mutate in cases.items():
            with self.subTest(code=code):
                value = self.envelope()
                value["evaluation_id"] = code
                mutate(value)
                self.bind(value)
                response = self.post(value)
                self.assertEqual(response.status_code, 200, response.text)
                receipt = response.json()["receipt"]
                self.assertEqual(receipt["outcome"], "denied")
                self.assertIn(code, [finding["code"] for finding in receipt["findings"]])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_missing_repository_and_inventory_overlap(self) -> None:
        result = self.post(self.envelope("repo", "admitted")).json()["receipt"]
        self.assertEqual(result["findings"][0]["code"], "repository-not-present")
        self.write("components", {"components": {"example": {}}})
        self.commit()
        value = self.envelope()
        result = self.post(value).json()["receipt"]
        self.assertIn("inventory-overlap", [item["code"] for item in result["findings"]])

    def test_real_git_ignores_dirty_worktree_and_denies_stale_merged_revision(self) -> None:
        value = self.envelope()
        path = self.repo / "contracts/intake-register.yaml"
        path.write_text("invalid: local work is not authority", encoding="utf-8")
        result = self.post(value)
        self.assertEqual(result.json()["receipt"]["outcome"], "allowed")
        self.assertEqual(path.read_text(), "invalid: local work is not authority")
        self.git("restore", "contracts/intake-register.yaml")
        (self.repo / "README.md").write_text("New unrelated merged source.", encoding="utf-8")
        self.commit()
        value["evaluation_id"] = "evaluation:after-merge"
        self.bind(value)
        result = self.post(value).json()["receipt"]
        self.assertEqual(result["outcome"], "denied")
        self.assertEqual(result["findings"][0]["code"], "stale-authority")

    def test_real_git_unmerged_policy_and_changed_contract_fail_closed(self) -> None:
        self.git("checkout", "-b", "unmerged")
        self.write("components", {"components": {"example": {}}})
        self.commit()
        value = self.envelope()
        result = self.post(value).json()["receipt"]
        self.assertEqual(result["authority"]["revision"], self.pin)
        self.assertEqual(result["outcome"], "denied")
        self.git("checkout", "main")
        path = self.repo / "contracts/intake-policy.yaml"
        path.write_text(path.read_text() + "\n# Changed policy revision.\n", encoding="utf-8")
        self.commit()
        value = self.envelope()
        value["evaluation_id"] = "evaluation:changed-contract"
        response = self.post(self.bind(value))
        self.assertEqual(response.status_code, 503)

    def test_api_auth_integrity_size_and_identity_conflict(self) -> None:
        value = self.envelope()
        path = "/v1/readiness/workspace-intake"
        self.assertEqual(self.client.post(path, json=value).status_code, 401)
        bad = copy.deepcopy(value)
        bad["request"]["owner_route"] = "changed"
        self.assertEqual(self.post(bad).status_code, 422)
        self.assertEqual(self.client.post(path, content=b"x" * 65537, headers=self.headers).status_code, 413)
        self.assertEqual(self.client.post(path, content=b'{"schema_version":1,"schema_version":1}', headers=self.headers).status_code, 422)
        self.assertEqual(self.post(value).status_code, 200)
        bad = copy.deepcopy(value)
        bad["execution_ref"] = "execution:other"
        self.assertEqual(self.post(self.bind(bad)).status_code, 409)
        with self.assertRaises(IntakeConflict):
            self.service.issue(canonical_bytes(value), actor="other-caller")

    def test_durable_receipt_tampering_fails_closed(self) -> None:
        value = self.envelope()
        result = self.post(value).json()
        token = result["receipt"]["receipt_digest"].removeprefix("sha256:")
        with self.sessions.begin() as session:
            row = session.get(WorkspaceIntakeEvaluationRecord, value["evaluation_id"])
            row.receipt = {**row.receipt, "outcome": "denied"}
        response = self.client.get(f"/v1/readiness/workspace-intake/{token}", headers=self.headers)
        self.assertEqual(response.status_code, 503)

    def test_default_activation_denied_even_with_environment_switch(self) -> None:
        with patch.dict("os.environ", {
            "WGCF_RUNTIME_PROFILE": "dev-integration",
            "WGCF_WORKSPACE_INTAKE_READINESS_ENABLED": "true",
        }):
            with self.assertRaises(IntakeUnavailable):
                build_workspace_intake_readiness_runtime()

    def test_bundle_and_committed_authority_integrity(self) -> None:
        root = self.root / "bundle"
        root.mkdir()
        source = Path(__file__).resolve().parents[1] / "contracts/workspace-intake"
        for path in source.iterdir():
            (root / path.name).write_bytes(path.read_bytes())
        with patch.dict("os.environ", {"WGCF_WORKSPACE_INTAKE_CONTRACT_ROOT": str(root)}):
            self.assertEqual(IntakeContracts.load().manifest, IntakeContracts.load(root).manifest)
        path = root / "workspace-intake-request.schema.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(IntakeUnavailable):
            IntakeContracts.load(root)
        self.write("intake-register", {"schema_version": 1})
        self.commit()
        with self.assertRaises(IntakeUnavailable):
            self.authority.snapshot()

    def applied_entry(self, value: dict) -> dict:
        request, decision = value["request"], value["decision"]
        record = {key: child for key, child in request["requested_record"].items() if key != "kind"}
        return {
            **record, "status": request["requested_classification"],
            "decision_source": decision["decision_source"], "owner_route": request["owner_route"],
            "record": {
                "id": request["target"]["record_id"], "version": 1, "source": request["source"],
                "decision": {
                    "id": decision["decision_id"], "ref": "decision:test", "digest": decision["decision_digest"],
                    "source": "operator", "operator_ref": "operator:test", "decided_at": decision["decided_at"],
                },
                "last_mutation": {
                    "id": "mutation:test", "idempotency_key": request["idempotency_key"],
                    "request_ref": "request:test", "request_digest": request["request_digest"],
                    "decision_ref": "decision:test", "decision_digest": decision["decision_digest"],
                    "applied_at": "2026-09-05T10:05:00Z",
                },
            },
        }

    def test_update_stale_version_source_immutability_and_canonical_replay(self) -> None:
        applied = self.envelope()
        entry = self.applied_entry(applied)
        self.register["components"]["example"] = entry
        self.write("intake-register", self.register)
        self.commit()
        applied["authority_revision"] = self.git("rev-parse", "HEAD")
        self.bind(applied)
        replay = self.post(applied).json()["receipt"]
        self.assertTrue(replay["observed_state"]["canonical_replay"])
        self.assertEqual(replay["outcome"], "allowed")
        self.assertEqual(replay["next_action"], "read-merged-record")

        value = self.envelope()
        value["evaluation_id"] = "evaluation:update"
        request = value["request"]
        request["action"] = "update"
        request["idempotency_key"] = "new-update"
        request["expected_state"].update(record_version=1, record_digest=digest(entry))
        self.bind(value)
        self.assertEqual(self.post(value).json()["receipt"]["outcome"], "allowed")
        for suffix, edit, expected in (
            ("stale", lambda r: r["expected_state"].update(record_version=2), "stale-record"),
            ("source", lambda r: r["source"].update(ref="source:other"), "source-identity-changed"),
            ("key", lambda r: r.update(idempotency_key=applied["request"]["idempotency_key"]), "idempotency-conflict"),
        ):
            candidate = copy.deepcopy(value)
            candidate["evaluation_id"] += suffix
            edit(candidate["request"])
            self.bind(candidate)
            receipt = self.post(candidate).json()["receipt"]
            self.assertEqual(receipt["outcome"], "denied")
            self.assertIn(expected, [finding["code"] for finding in receipt["findings"]])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def ai_envelope(self) -> dict:
        value = self.envelope()
        decision = value["decision"]
        decision["decision_source"] = "ai-suggested"
        decision["ai_suggestion"] = {
            "profile_id": "profile:test", "policy_status": "active", "decision_id": "ai:test",
            "generated_at": "2026-09-05T10:00:30Z", "confidence": "high",
            "caller_id": "operator-orchestration-service", "invocation_path": "workspace-intake",
            "suggested_decision": "proposed", "operator_decision": "proposed",
            "acceptance_state": "accepted", "accepted_by": "operator:test",
            "accepted_at": "2026-09-05T10:01:00Z", "audit_ref": "audit:test",
        }
        return self.bind(value)

    def test_ai_active_profile_exact_acceptance_override_and_reuse(self) -> None:
        value = self.ai_envelope()
        denied = self.post(value).json()["receipt"]
        self.assertEqual(denied["outcome"], "denied")
        self.write("governed-intake-assist", {"governed_intake_assist": {
            "activation_state": {"source_contract_status": "active", "live_consumption_allowed": True},
            "consumer": {
                "profile_id": "profile:test", "caller_id": "operator-orchestration-service",
                "invocation_path": "workspace-intake",
            },
        }})
        self.commit()
        accepted = self.ai_envelope()
        accepted["evaluation_id"] = "evaluation:ai:accepted"
        self.bind(accepted)
        self.assertEqual(self.post(accepted).json()["receipt"]["outcome"], "allowed")
        overridden = copy.deepcopy(accepted)
        overridden["evaluation_id"] = "evaluation:ai:overridden"
        overridden["decision"]["ai_suggestion"].update(
            acceptance_state="overridden", suggested_decision="admitted", override_reason="Propose only.",
        )
        self.bind(overridden)
        self.assertEqual(self.post(overridden).json()["receipt"]["outcome"], "allowed")
        overridden["evaluation_id"] += ":wrong"
        overridden["decision"]["ai_suggestion"]["accepted_by"] = "operator:other"
        self.bind(overridden)
        self.assertEqual(self.post(overridden).json()["receipt"]["outcome"], "denied")
        entry = self.applied_entry(accepted)
        entry["ai_suggestion"] = accepted["decision"]["ai_suggestion"]
        entry["record"]["decision"]["source"] = "ai-suggested"
        self.register["components"]["example"] = entry
        self.write("intake-register", self.register)
        self.commit()
        value = self.ai_envelope()
        value["evaluation_id"] = "evaluation:ai:reused"
        value["request"]["idempotency_key"] = "new-mutation"
        self.bind(value)
        receipt = self.post(value).json()["receipt"]
        self.assertIn("ai-decision-reused", [finding["code"] for finding in receipt["findings"]])

    def test_disposition_contract_enforces_evidence_and_deferral(self) -> None:
        for disposition in ("remove", "workaround", "accept-risk", "defer"):
            value = self.envelope()
            value["evaluation_id"] += disposition
            finding = {
                "code": "reviewed-finding", "severity": "blocking", "message": "Review finding.",
                "disposition": disposition, "justification": "Reviewed by the responsible authority.",
                "owner_ref": "owner:test" if disposition in {"workaround", "defer"} else None,
                "review_due_on": "2026-09-06" if disposition in {"workaround", "defer"} else None,
                "evidence_refs": ["evidence:test"],
                "security_evidence_refs": ["security:test"] if disposition == "accept-risk" else [],
            }
            value["decision"]["outcome"]["findings"] = [finding]
            if disposition == "defer":
                value["decision"]["outcome"]["status"] = "requires-action"
            self.bind(value)
            response = self.post(value)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["receipt"]["outcome"], "requires-action" if disposition == "defer" else "allowed")
            if disposition == "accept-risk":
                finding["security_evidence_refs"] = []
                value["evaluation_id"] += ":invalid"
                self.bind(value)
                self.assertEqual(self.post(value).status_code, 422)

    def test_api_unknown_receipt_cross_caller_and_storage_failure(self) -> None:
        receipt = self.post(self.envelope()).json()["receipt"]
        token = receipt["receipt_digest"].removeprefix("sha256:")
        other = {"x-wgcf-caller-id": "workspace-governance-control-fabric", "x-wgcf-caller-secret": "r" * 32}
        self.assertEqual(self.client.get(f"/v1/readiness/workspace-intake/{token}", headers=other).status_code, 404)
        self.assertEqual(self.client.get(f"/v1/readiness/workspace-intake/{'0' * 64}", headers=self.headers).status_code, 404)
        metadata.drop_all(self.engine)
        self.assertEqual(self.post(self.envelope("product")).status_code, 503)

    def test_canonicalization_matches_workspace_unicode_order_and_no_floats(self) -> None:
        value = {"\U00010000": 1, "\ue000": 2}
        expected = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        self.assertEqual(canonical_bytes(value), expected)
        with self.assertRaises(IntakeRequestError):
            canonical_bytes({"float": 1.1})
