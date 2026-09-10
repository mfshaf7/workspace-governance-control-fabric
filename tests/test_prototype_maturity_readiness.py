from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
import yaml

from control_fabric_core.artifact_registry import ArtifactRegistryAuthorizer
from control_fabric_core.db.models import LedgerEvent, PrototypeMaturityReadinessRecord, metadata
from control_fabric_core.prototype_maturity_contracts import (
    PrototypeMaturityAuthority,
    PrototypeMaturityContracts,
    PrototypeMaturityUnavailable,
    artifact_digest,
    canonical_bytes,
    digest,
)
from control_fabric_core.prototype_maturity_readiness import (
    PrototypeMaturityConflict,
    PrototypeMaturityReadinessService,
    build_prototype_maturity_readiness_runtime,
)
from wgcf_api.app import create_app


REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-10T03:00:00Z"
FIXED_TIME = datetime(2026, 9, 10, 3, tzinfo=timezone.utc)
CHECKS = (
    "request-integrity",
    "lifecycle-source-state",
    "source-version-freshness",
    "packet-integrity",
    "required-evidence",
    "boundary-coherence",
    "security-trigger-disposition",
    "open-issue-disposition",
)
CONSUMER_SAFE_EVIDENCE_REF = re.compile(
    r"^(?:record|repo|openproject|evidence|proof|security-review|console|wgcf)://"
    r"[A-Za-z0-9][A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*$"
)
SECTIONS = {
    "candidate-promotion": (
        "candidate-brief", "scope-and-non-goals", "boundaries-and-risks",
    ),
    "baseline-promotion": (
        "definition", "design-and-workflow", "evidence", "boundaries",
        "issues-and-risk-disposition",
    ),
}


class PrototypeMaturityReadinessTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "workspace-prototype-studio"
        (self.repo / "contracts/prototype-maturity").mkdir(parents=True)
        (self.repo / "schemas").mkdir()
        (self.repo / "docs/prototypes/sample-tool").mkdir(parents=True)
        (self.repo / "records/prototype-landings/sample-tool").mkdir(parents=True)
        shutil.copy2(
            REPO_ROOT / "contracts/prototype-maturity/source-authority-manifest.json",
            self.repo / "contracts/prototype-maturity/manifest.json",
        )
        for name in ("prototype-registry.schema.json", "prototype-candidate-record.schema.json"):
            shutil.copy2(REPO_ROOT / "contracts/prototype-maturity" / name, self.repo / "schemas" / name)
        (self.repo / "docs/prototypes/sample-tool/brief.md").write_text(
            "# Sample tool\n\nCommitted source context.\n", encoding="utf-8"
        )
        (self.repo / "records/prototype-landings/sample-tool/record.json").write_text(
            json.dumps({
                "id": "prototype:sample-tool",
                "entry_ref": {
                    "id": "prototype-entry:direct:sample-tool",
                    "digest": "sha256:" + "0" * 64,
                },
                "name": "Sample Tool",
                "objective": "Prove deterministic maturity readiness.",
                "ingress_class": "direct",
                "lifecycle": "exploring",
                "project_phase": "incubating",
                "setup": {"support_profile": "simple"},
                "source": {
                    "posture": "create-studio-source",
                    "ref": "repo://workspace-prototype-studio/prototypes/sample-tool",
                    "revision": "created-from:test",
                },
                "next_action": "candidate-promotion",
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        self.write_registry("exploring")
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Prototype Maturity Test")
        self.git("config", "user.email", "maturity@example.invalid")
        self.commit("Initialize Prototype maturity source")

        contracts = PrototypeMaturityContracts.load()
        self.contracts = replace(
            contracts,
            manifest={
                **contracts.manifest,
                "source_authority": {
                    **contracts.manifest["source_authority"],
                    "minimum_commit": self.git("rev-parse", "HEAD"),
                },
            },
        )
        self.authority = PrototypeMaturityAuthority(
            self.repo, self.contracts, trusted_ref="refs/heads/main"
        )
        self.engine = create_engine(f"sqlite:///{self.root / 'ledger.sqlite'}")
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.service = self.new_service()
        self.client = TestClient(
            create_app(
                prototype_maturity_readiness=self.service,
                artifact_registry_authorizer=ArtifactRegistryAuthorizer(
                    oos_secret="o" * 32, reconciler_secret="r" * 32
                ),
            )
        )
        self.addCleanup(self.client.close)
        self.headers = {
            "x-wgcf-caller-id": "operator-orchestration-service",
            "x-wgcf-caller-secret": "o" * 32,
        }

    def new_service(self, *, clock=lambda: FIXED_TIME) -> PrototypeMaturityReadinessService:
        return PrototypeMaturityReadinessService(
            session_factory=self.sessions,
            authority=self.authority,
            service_identity_ref="spiffe://test/wgcf/prototype-maturity",
            implementation_ref="2" * 40,
            clock=clock,
        )

    def assert_consumer_safe_evidence(self, result: dict) -> None:
        for check in result["readiness"]["checks"]:
            self.assertTrue(check["evidence_refs"])
            for evidence_ref in check["evidence_refs"]:
                self.assertRegex(evidence_ref, CONSUMER_SAFE_EVIDENCE_REF)

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self, message: str) -> None:
        self.git("add", ".")
        self.git("commit", "-m", message)

    def record(self, lifecycle: str, *, candidate: bool = False) -> dict:
        links = [{
            "role": "landing-record",
            "ref": "record://prototype-landings/sample-tool",
            "system": "prototype-studio",
            "level": "record",
            "label": "Prototype Landing record",
        }]
        if candidate:
            links.append({
                "role": "candidate-record",
                "ref": "record://prototype-maturity/sample-tool/candidate",
                "system": "prototype-studio",
                "level": "record",
                "label": "Candidate Promotion record",
            })
        return {
            "id": "sample-tool",
            "name": "Sample Tool",
            "objective": "Prove deterministic maturity readiness.",
            "portfolio": "experiment",
            "lifecycle": lifecycle,
            "project_phase": "incubating",
            "owner": "Workspace Prototype Studio",
            "visibility_tier": "private-internal",
            "data_mode": "synthetic",
            "mutation_boundary": "none",
            "landing_record_ref": "record://prototype-landings/sample-tool",
            "linked_records": links,
            "paths": {"brief": "docs/prototypes/sample-tool/brief.md"},
        }

    def write_registry(self, lifecycle: str, *, candidate: bool = False) -> None:
        (self.repo / "prototypes.yaml").write_text(
            yaml.safe_dump({
                "schema_version": 1,
                "studio": {"owner_repo": "workspace-prototype-studio"},
                "prototypes": [self.record(lifecycle, candidate=candidate)],
            }, sort_keys=False),
            encoding="utf-8",
        )

    def promote_to_candidate(self) -> None:
        self.write_registry("candidate", candidate=True)
        path = self.repo / "records/prototype-maturity/sample-tool/candidate.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        candidate = {
            "schema_version": 1,
            "artifact_type": "prototype-candidate-record",
            "record_id": "prototype-candidate:sample-tool:1",
            "prototype_id": "prototype:sample-tool",
            "promoted_at": NOW,
            "promoted_by": "operator:test",
            "request_ref": {"id": "request:test", "digest": "sha256:" + "1" * 64},
            "packet_ref": {"id": "packet:test", "digest": "sha256:" + "2" * 64},
            "readiness_ref": {"id": "readiness:test", "digest": "sha256:" + "3" * 64},
            "decision_ref": {"id": "decision:test", "digest": "sha256:" + "4" * 64},
            "expected_state": {
                "source_revision": "5" * 40,
                "record_digest": "sha256:" + "6" * 64,
                "lifecycle": "exploring",
            },
            "accepted_values": self.editable_values("candidate-promotion"),
        }
        candidate["record_digest"] = digest(candidate)
        path.write_text(json.dumps(candidate, indent=2) + "\n", encoding="utf-8")
        self.commit("Promote sample tool to candidate")

    @staticmethod
    def editable_values(transition: str) -> dict:
        if transition == "candidate-promotion":
            return {
                "prototype-objective": "Prove deterministic maturity readiness.",
                "target-user": "Workspace operator",
                "expected-proof": "A repeatable local demonstration.",
                "accepted-scope": ["Readiness evaluation"],
                "excluded-scope": ["Runtime activation"],
                "boundary-clarifications": "Prototype Studio retains source authority.",
                "open-issue-disposition": "No open blocking issues.",
            }
        return {
            "baseline-title": "Sample Tool Baseline",
            "baseline-statement": "The local design and evidence are accepted.",
            "accepted-summary": "Deterministic maturity behavior.",
            "excluded-summary": "Delivery and runtime authority.",
            "selected-evidence-refs": [
                "repo://workspace-prototype-studio/docs/prototypes/sample-tool/brief.md"
            ],
            "missing-evidence-disposition": "No required evidence is missing.",
            "issue-and-risk-disposition": "No open blocking issues.",
        }

    @staticmethod
    def bind(value: dict, field: str) -> dict:
        result = copy.deepcopy(value)
        result[field] = artifact_digest(result, field)
        return result

    def envelope(self, transition: str = "candidate-promotion", *, sequence: int = 1) -> dict:
        revision = self.git("rev-parse", "HEAD")
        registry = yaml.safe_load((self.repo / "prototypes.yaml").read_text(encoding="utf-8"))
        record = registry["prototypes"][0]
        source, target = (
            ("exploring", "candidate")
            if transition == "candidate-promotion"
            else ("candidate", "baseline-approved")
        )
        source_refs = [
            "record://prototype-landings/sample-tool",
            "repo://workspace-prototype-studio/docs/prototypes/sample-tool/brief.md",
        ]
        if transition == "baseline-promotion":
            source_refs.append("record://prototype-maturity/sample-tool/candidate")
        request = self.bind({
            "schema_version": 1,
            "artifact_type": "prototype-maturity-request",
            "request_id": f"prototype-maturity-request:sample-tool:{sequence}",
            "requested_at": NOW,
            "operator_ref": "operator:test",
            "prototype_id": "prototype:sample-tool",
            "transition": transition,
            "source_lifecycle": source,
            "target_lifecycle": target,
            "expected_state": {
                "source_revision": revision,
                "record_digest": digest(record),
                "lifecycle": source,
            },
            "inputs": {
                "source_refs": source_refs,
                "editable_values": self.editable_values(transition),
            },
            "correlation_id": f"correlation:sample-tool:{sequence}",
            "idempotency_key": f"maturity:sample-tool:{sequence}",
        }, "request_digest")
        packet = self.bind({
            "schema_version": 1,
            "artifact_type": "prototype-maturity-packet",
            "packet_id": f"prototype-maturity-packet:sample-tool:{sequence}",
            "assembled_at": NOW,
            "request_ref": {"id": request["request_id"], "digest": request["request_digest"]},
            "prototype_id": "prototype:sample-tool",
            "transition": transition,
            "packet_kind": (
                "candidate-evidence-packet"
                if transition == "candidate-promotion"
                else "baseline-packet"
            ),
            "sections": [{
                "id": section,
                "state": "ready",
                "evidence_refs": [
                    "repo://workspace-prototype-studio/docs/prototypes/sample-tool/brief.md"
                ],
            } for section in SECTIONS[transition]],
        }, "packet_digest")
        return self.bind({
            "schema_version": 1,
            "artifact_type": "wgcf-prototype-maturity-evaluation",
            "evaluation_id": f"prototype-maturity-evaluation:sample-tool:{sequence}",
            "session_ref": "session:test",
            "execution_ref": "execution:test",
            "authority_revision": revision,
            "policy_ref": self.contracts.policy_ref,
            "security_review_ref": self.contracts.security_review_ref,
            "request": request,
            "packet": packet,
        }, "evaluation_digest")

    def rebind(self, value: dict) -> dict:
        value["request"] = self.bind(value["request"], "request_digest")
        value["packet"]["request_ref"] = {
            "id": value["request"]["request_id"],
            "digest": value["request"]["request_digest"],
        }
        value["packet"] = self.bind(value["packet"], "packet_digest")
        return self.bind(value, "evaluation_digest")

    def post(self, value: dict):
        return self.client.post(
            "/v1/readiness/prototype-maturity",
            content=canonical_bytes(value),
            headers=self.headers,
        )

    def test_candidate_and_baseline_are_durable_and_do_not_mutate_source(self) -> None:
        candidate = self.envelope()
        before = self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")
        response = self.post(candidate)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["readiness"]["outcome"], "ready")
        self.assertEqual([item["id"] for item in result["readiness"]["checks"]], list(CHECKS))
        self.assert_consumer_safe_evidence(result)
        self.assertEqual(result["ledger"]["resolution"], "created")
        replay = self.service.issue(canonical_bytes(candidate), actor="operator-orchestration-service")
        self.assertEqual(replay["ledger"]["resolution"], "reused")
        self.assertEqual((self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")), before)

        self.promote_to_candidate()
        baseline = self.envelope("baseline-promotion", sequence=2)
        before = self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")
        response = self.post(baseline)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["readiness"]["outcome"], "ready")
        self.assert_consumer_safe_evidence(response.json())
        self.assertEqual((self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")), before)

        token = response.json()["readiness"]["readiness_digest"].removeprefix("sha256:")
        readback = self.client.get(
            f"/v1/readiness/prototype-maturity/{token}", headers=self.headers
        )
        self.assertEqual(readback.status_code, 200, readback.text)
        with self.sessions() as session:
            self.assertEqual(len(session.scalars(select(PrototypeMaturityReadinessRecord)).all()), 2)
            self.assertEqual(len(session.scalars(select(LedgerEvent)).all()), 4)

    def test_missing_evidence_and_invalid_lifecycle_block(self) -> None:
        missing = self.envelope(sequence=3)
        missing["request"]["inputs"]["source_refs"][1] = (
            "repo://workspace-prototype-studio/docs/prototypes/sample-tool/missing.md"
        )
        response = self.post(self.rebind(missing))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["readiness"]["outcome"], "blocked")
        self.assertIn("maturity-evidence-unresolved", response.text)

        self.promote_to_candidate()
        invalid = self.envelope("candidate-promotion", sequence=4)
        response = self.post(invalid)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["readiness"]["outcome"], "blocked")
        self.assertIn("prototype-lifecycle-transition-invalid", response.text)

    def test_stale_source_policy_and_security_bindings_are_explicit(self) -> None:
        cases = {
            "source": lambda value: value.update(authority_revision="0" * 40),
            "policy": lambda value: value["policy_ref"].update(digest="sha256:" + "7" * 64),
            "security": lambda value: value["security_review_ref"].update(
                digest="sha256:" + "8" * 64
            ),
        }
        for sequence, (name, mutate) in enumerate(cases.items(), start=10):
            with self.subTest(name=name):
                value = self.envelope(sequence=sequence)
                mutate(value)
                response = self.post(self.bind(value, "evaluation_digest"))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["readiness"]["outcome"], "stale")

    def test_unsupported_evidence_and_prohibited_operator_content_block(self) -> None:
        unsupported = self.envelope(sequence=15)
        unsupported["request"]["inputs"]["source_refs"][1] = (
            "evidence://unadmitted-owner/sample-tool"
        )
        response = self.post(self.rebind(unsupported))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["readiness"]["outcome"], "blocked")

        prohibited = self.envelope(sequence=16)
        prohibited["request"]["inputs"]["editable_values"]["prototype-objective"] = (
            "-----BEGIN PRIVATE KEY-----"
        )
        response = self.post(self.rebind(prohibited))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["readiness"]["outcome"], "blocked")
        self.assertIn("maturity-boundary-invalid", response.text)

    def test_tampered_source_contract_is_unavailable(self) -> None:
        path = self.repo / "contracts/prototype-maturity/manifest.json"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.commit("Tamper Prototype maturity source contract")

        response = self.post(self.envelope(sequence=17))

        self.assertEqual(response.status_code, 503, response.text)

    def test_api_auth_idempotency_caller_isolation_and_expiry(self) -> None:
        value = self.envelope(sequence=20)
        path = "/v1/readiness/prototype-maturity"
        self.assertEqual(self.client.post(path, json=value).status_code, 401)
        self.assertEqual(
            self.client.post(path, content=b"x" * (256 * 1024 + 1), headers=self.headers).status_code,
            413,
        )
        response = self.post(value)
        self.assertEqual(response.status_code, 200, response.text)
        conflict = copy.deepcopy(value)
        conflict["execution_ref"] = "execution:changed"
        conflict = self.bind(conflict, "evaluation_digest")
        self.assertEqual(self.post(conflict).status_code, 409)
        with self.assertRaises(PrototypeMaturityConflict):
            self.service.issue(canonical_bytes(value), actor="other-caller")
        token = response.json()["readiness"]["readiness_digest"].removeprefix("sha256:")
        other_headers = {
            "x-wgcf-caller-id": "workspace-governance-control-fabric",
            "x-wgcf-caller-secret": "r" * 32,
        }
        self.assertEqual(
            self.client.get(
                f"/v1/readiness/prototype-maturity/{token}", headers=other_headers
            ).status_code,
            404,
        )
        expired = self.new_service(clock=lambda: FIXED_TIME + timedelta(minutes=16))
        self.assertEqual(
            expired.read(token, actor="operator-orchestration-service")["ledger"]["state"],
            "expired",
        )

    def test_source_activation_is_pinned_to_approved_evidence(self) -> None:
        contracts = PrototypeMaturityContracts.load()
        self.assertTrue(contracts.manifest["runtime_activation"])
        self.assertEqual(
            contracts.manifest["activation_review"],
            {
                "repo": "security-architecture",
                "commit": "087118a5f79034684f0ca895a85cb735d1298627",
                "path": (
                    "docs/reviews/components/"
                    "2026-09-10-prototype-maturity-normal-availability.md"
                ),
                "content_sha256": (
                    "e0923786d5e19f7843ec4a8941fc007c"
                    "701dec55e7b3d523695d2049923665da"
                ),
                "decision": "approved-with-findings",
            },
        )
        self.assertEqual(
            contracts.manifest["activation_evidence"],
            {
                "conformance_review_packet": {
                    "uri": (
                        "wgcf://artifacts/delivery-art/sha256/"
                        "1267af69967d433caea791dbadc600bef718b8489ddd9ac5778799431d86f01f"
                    ),
                    "digest": (
                        "sha256:1267af69967d433caea791dbadc600bef"
                        "718b8489ddd9ac5778799431d86f01f"
                    ),
                },
                "identity_review_packet": {
                    "uri": (
                        "wgcf://artifacts/delivery-art/sha256/"
                        "1a1ec22ccd4456db99f67157c084a3c46ce4fb03ed1f4be820afadd60e676c45"
                    ),
                    "digest": (
                        "sha256:1a1ec22ccd4456db99f67157c084a3c4"
                        "6ce4fb03ed1f4be820afadd60e676c45"
                    ),
                },
                "identity_definition": {
                    "repo": "platform-engineering",
                    "commit": "f2b3b5f0f96b13217487250fd16d59dc77496d16",
                    "path": "security/prototype-maturity-identity.yaml",
                    "content_sha256": (
                        "a951e0c46de67cd53e362075d0c2d585"
                        "a56678bfdf4031de04c97d09d692738c"
                    ),
                },
            },
        )

    def test_malformed_activation_evidence_is_unavailable(self) -> None:
        root = self.root / "tampered-prototype-maturity-contracts"
        shutil.copytree(REPO_ROOT / "contracts/prototype-maturity", root)
        path = root / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["activation_evidence"]["identity_review_packet"]["uri"] = (
            "wgcf://artifacts/delivery-art/sha256/" + "0" * 64
        )
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        with self.assertRaises(PrototypeMaturityUnavailable):
            PrototypeMaturityContracts.load(root)

        manifest["activation_evidence"]["identity_review_packet"] = []
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises(PrototypeMaturityUnavailable):
            PrototypeMaturityContracts.load(root)

    def test_runtime_requires_profile_and_explicit_environment_activation(self) -> None:
        with patch.dict(
            "os.environ",
            {"WGCF_RUNTIME_PROFILE": "dev-integration"},
            clear=True,
        ):
            with self.assertRaises(PrototypeMaturityUnavailable):
                build_prototype_maturity_readiness_runtime()
        with patch.dict(
            "os.environ",
            {
                "WGCF_RUNTIME_PROFILE": "stage",
                "WGCF_PROTOTYPE_MATURITY_READINESS_ENABLED": "true",
                "WGCF_PROTOTYPE_STUDIO_REPO_ROOT": str(self.repo),
            },
            clear=True,
        ):
            with self.assertRaises(PrototypeMaturityUnavailable):
                build_prototype_maturity_readiness_runtime()
        with patch.dict(
            "os.environ",
            {
                "WGCF_RUNTIME_PROFILE": "dev-integration",
                "WGCF_PROTOTYPE_MATURITY_READINESS_ENABLED": "true",
                "WGCF_PROTOTYPE_STUDIO_REPO_ROOT": str(self.repo),
                "WGCF_PROTOTYPE_MATURITY_SERVICE_IDENTITY_REF": (
                    "service-identity://workspace-governance-control-fabric/"
                    "prototype-maturity/dev-integration"
                ),
                "WGCF_DATABASE_URL": f"sqlite:///{self.root / 'runtime.sqlite'}",
            },
            clear=True,
        ), patch(
            "control_fabric_core.prototype_maturity_readiness.read_implementation_ref",
            return_value="2" * 40,
        ):
            runtime = build_prototype_maturity_readiness_runtime()
            self.assertEqual(
                runtime.identity,
                "service-identity://workspace-governance-control-fabric/"
                "prototype-maturity/dev-integration",
            )
            self.assertEqual(runtime.implementation, "2" * 40)
