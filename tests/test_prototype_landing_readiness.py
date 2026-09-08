from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
from control_fabric_core.db.models import (
    LedgerEvent,
    PrototypeLandingReadinessRecord,
    metadata,
)
from control_fabric_core.prototype_landing_contracts import (
    PrototypeLandingAuthority,
    PrototypeLandingContracts,
    PrototypeLandingUnavailable,
    artifact_digest,
    canonical_bytes,
    digest,
)
from control_fabric_core.prototype_landing_readiness import (
    PrototypeLandingConflict,
    PrototypeLandingReadinessService,
    build_prototype_landing_readiness_runtime,
)
from wgcf_api.app import create_app


REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-07T06:30:00Z"
FIXED_TIME = datetime(2026, 9, 7, 6, 30, tzinfo=timezone.utc)
UPSTREAM_DIGEST = "sha256:" + "1" * 64
DIMENSIONS = (
    "source", "studio-home", "interface", "runtime", "data",
    "integration", "tooling", "evidence", "visibility", "recovery",
)
CHECKS = (
    "entry-integrity", "identity-availability", "required-metadata",
    "support-profile-integrity", "support-row-readiness",
    "source-custody-coherence", "source-version-freshness",
    "data-and-mutation-boundary", "visibility-and-exposure",
    "security-trigger-disposition", "expected-mutation-set",
)


class PrototypeLandingReadinessTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "workspace-prototype-studio"
        self.repo.mkdir()
        shutil.copytree(
            REPO_ROOT / "contracts" / "prototype-landing",
            self.repo / "contracts" / "prototype-landing",
        )
        (self.repo / "schemas").mkdir()
        shutil.copy2(
            REPO_ROOT / "contracts/prototype-landing/prototype-registry.schema.json",
            self.repo / "schemas/prototype-registry.schema.json",
        )
        self.write_registry([])
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Prototype Landing Test")
        self.git("config", "user.email", "landing@example.invalid")
        self.commit("Initialize Prototype Studio authority")

        contracts = PrototypeLandingContracts.load()
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
        self.authority = PrototypeLandingAuthority(
            self.repo, self.contracts, trusted_ref="refs/heads/main"
        )
        self.engine = create_engine(f"sqlite:///{self.root / 'ledger.sqlite'}")
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.service = self.new_service()
        self.client = TestClient(
            create_app(
                prototype_landing_readiness=self.service,
                artifact_registry_authorizer=ArtifactRegistryAuthorizer(
                    oos_secret="o" * 32,
                    reconciler_secret="r" * 32,
                ),
            )
        )
        self.addCleanup(self.client.close)
        self.headers = {
            "x-wgcf-caller-id": "operator-orchestration-service",
            "x-wgcf-caller-secret": "o" * 32,
        }

    def new_service(
        self, *, clock=lambda: FIXED_TIME
    ) -> PrototypeLandingReadinessService:
        return PrototypeLandingReadinessService(
            session_factory=self.sessions,
            authority=self.authority,
            service_identity_ref="spiffe://test/wgcf/prototype-landing",
            implementation_ref="2" * 40,
            clock=clock,
        )

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

    def write_registry(self, prototypes: list[dict]) -> None:
        path = self.repo / "prototypes.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "studio": {"owner_repo": "workspace-prototype-studio"},
                    "prototypes": prototypes,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def bind(value: dict, field: str) -> dict:
        result = copy.deepcopy(value)
        result[field] = artifact_digest(result, field)
        return result

    @staticmethod
    def ref(value: dict) -> dict:
        identity_fields = {
            "prototype-entry-packet": ("entry_id", "packet_digest"),
            "prototype-landing-request": ("request_id", "request_digest"),
            "prototype-landing-plan": ("plan_id", "plan_digest"),
        }
        identity, content = identity_fields[value["artifact_type"]]
        return {"id": value[identity], "digest": value[content]}

    def envelope(
        self,
        *,
        ingress_class: str = "direct",
        posture: str = "create-studio-source",
        source_ref: str = "repo://workspace-prototype-studio/prototypes/sample-tool",
        source_revision: str | None = None,
        origin_digest: str | None = None,
        imported_digest: str | None = None,
        data_mode: str = "synthetic",
        mutation_boundary: str = "none",
        visibility: str = "private",
        row_state: str | None = None,
        constraints: list[dict] | None = None,
        mutation_set: tuple[str, ...] | None = None,
    ) -> dict:
        revision = self.git("rev-parse", "HEAD")
        registry = yaml.safe_load((self.repo / "prototypes.yaml").read_text(encoding="utf-8"))
        entry = self.bind(
            {
                "schema_version": 1,
                "artifact_type": "prototype-entry-packet",
                "entry_id": f"prototype-entry:{ingress_class}:sample-tool",
                "captured_at": NOW,
                "ingress_class": ingress_class,
                "source": {
                    "authority": "operator" if ingress_class == "direct" else "workspace-proposals",
                    "ref": "source:sample-tool",
                    "digest": UPSTREAM_DIGEST,
                    "revision": None,
                },
                "suggestions": {
                    "name": "Suggested tool",
                    "objective": "Suggested objective",
                    "support_profile": "simple",
                },
                "constraints": constraints or [],
                "requested_by": "operator:test",
            },
            "packet_digest",
        )
        rows = [
            {
                "dimension": dimension,
                "state": row_state if row_state and dimension == "runtime" else (
                    "ready" if dimension in {"source", "studio-home", "evidence", "recovery"}
                    else "not-needed"
                ),
                "generated": True,
                "detail": f"{dimension} resolved by the simple profile",
            }
            for dimension in DIMENSIONS
        ]
        request = self.bind(
            {
                "schema_version": 1,
                "artifact_type": "prototype-landing-request",
                "request_id": "prototype-landing-request:sample-tool:1",
                "requested_at": NOW,
                "operator_ref": "operator:test",
                "entry_packet_ref": self.ref(entry),
                "prototype": {
                    "id": "prototype:sample-tool",
                    "name": "Operator Chosen Tool",
                    "objective": "Prove deterministic Prototype Landing.",
                },
                "setup": {
                    "support_profile": "simple",
                    "support_rows": rows,
                    "scaffold_profile": "python-library",
                    "preview_mode": "none",
                    "data_mode": data_mode,
                    "mutation_boundary": mutation_boundary,
                    "visibility": visibility,
                },
                "source_plan": {
                    "posture": posture,
                    "source_ref": source_ref,
                    "source_revision": source_revision,
                    "origin_digest": origin_digest,
                    "imported_content_digest": imported_digest,
                },
                "starting_lifecycle": "exploring",
                "expected_state": {
                    "registry_digest": digest(registry),
                    "record_present": False,
                    "record_digest": None,
                    "source_revision": revision,
                },
                "operator_accepted": True,
                "correlation_id": "correlation:sample-tool:1",
                "idempotency_key": "landing:sample-tool:1",
            },
            "request_digest",
        )
        if mutation_set is None:
            mutation_set = (
                "registry-record", "prototype-docs", "prototype-source", "validation-plan"
            )
        targets = {
            "registry-record": "prototypes.yaml",
            "prototype-docs": "docs/prototypes/sample-tool",
            "prototype-source": "prototypes/sample-tool",
            "fixtures": "fixtures/prototypes/sample-tool",
            "preview-profile-draft": "records/prototype-preview-profiles/sample-tool.yaml",
            "validation-plan": "records/prototype-landings/sample-tool/validation-plan.yaml",
        }
        plan = self.bind(
            {
                "schema_version": 1,
                "artifact_type": "prototype-landing-plan",
                "plan_id": "prototype-landing-plan:sample-tool:1",
                "planned_at": NOW,
                "request_ref": self.ref(request),
                "prototype_id": "prototype:sample-tool",
                "source_plan": copy.deepcopy(request["source_plan"]),
                "mutation_set": list(mutation_set),
                "expected_outputs": [
                    {"kind": kind, "target_ref": targets[kind], "required": True}
                    for kind in mutation_set
                ],
                "next_action": "candidate-promotion",
            },
            "plan_digest",
        )
        return self.bind(
            {
                "schema_version": 1,
                "artifact_type": "wgcf-prototype-landing-evaluation",
                "evaluation_id": "prototype-landing-evaluation:sample-tool:1",
                "session_ref": "session:test",
                "execution_ref": "execution:test",
                "authority_revision": revision,
                "entry_packet": entry,
                "request": request,
                "plan": plan,
            },
            "evaluation_digest",
        )

    def rebind(self, value: dict) -> dict:
        value["request"] = self.bind(value["request"], "request_digest")
        value["plan"]["request_ref"] = self.ref(value["request"])
        value["plan"]["source_plan"] = copy.deepcopy(value["request"]["source_plan"])
        value["plan"] = self.bind(value["plan"], "plan_digest")
        return self.bind(value, "evaluation_digest")

    def post(self, value: dict):
        return self.client.post(
            "/v1/readiness/prototype-landing",
            content=canonical_bytes(value),
            headers=self.headers,
        )

    def test_ready_direct_and_proposal_routes_are_durable_and_non_mutating(self) -> None:
        before = self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")
        for index, ingress_class in enumerate(("direct", "proposal-routed"), start=1):
            with self.subTest(ingress_class=ingress_class):
                value = self.envelope(ingress_class=ingress_class)
                value["evaluation_id"] = f"prototype-landing-evaluation:sample-tool:{index}"
                value = self.bind(value, "evaluation_digest")
                response = self.post(value)
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                readiness = result["readiness"]
                self.assertEqual(readiness["outcome"], "ready")
                self.assertEqual([item["id"] for item in readiness["checks"]], list(CHECKS))
                self.assertTrue(all(item["state"] == "ready" for item in readiness["checks"]))
                self.assertEqual(result["ledger"]["state"], "durable")
                self.assertEqual(result["ledger"]["authority_revision"], before[1])
                self.assertEqual(result["ledger"]["implementation_ref"], "2" * 40)
                self.assertIn("expires_at", result["ledger"])
                replay = self.new_service().issue(
                    canonical_bytes(value), actor="operator-orchestration-service"
                )
                self.assertEqual(replay["ledger"]["resolution"], "reused")
                token = readiness["readiness_digest"].removeprefix("sha256:")
                readback = self.client.get(
                    f"/v1/readiness/prototype-landing/{token}", headers=self.headers
                )
                self.assertEqual(readback.json()["readiness"], readiness)
        self.assertEqual(
            (self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")), before
        )
        with self.sessions() as session:
            self.assertEqual(
                len(session.scalars(select(PrototypeLandingReadinessRecord)).all()), 2
            )
            self.assertEqual(len(session.scalars(select(LedgerEvent)).all()), 6)

    def test_existing_studio_source_can_be_ready_without_source_mutation(self) -> None:
        source = self.repo / "prototypes" / "sample-tool"
        source.mkdir(parents=True)
        (source / "README.md").write_text("existing source\n", encoding="utf-8")
        self.commit("Add existing source")
        revision = self.git("rev-parse", "HEAD")
        value = self.envelope(
            posture="use-existing-studio-source",
            source_revision=revision,
            mutation_set=("registry-record", "prototype-docs", "validation-plan"),
        )
        before = self.git("status", "--porcelain"), revision
        readiness = self.post(value).json()["readiness"]
        self.assertEqual(readiness["outcome"], "ready")
        self.assertEqual(
            (self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")), before
        )

    def test_needed_support_is_preserved_without_blocking_landing(self) -> None:
        value = self.envelope(row_state="needed")

        response = self.post(value)

        self.assertEqual(response.status_code, 200, response.text)
        readiness = response.json()["readiness"]
        self.assertEqual(readiness["outcome"], "ready")
        support_check = next(
            item for item in readiness["checks"] if item["id"] == "support-row-readiness"
        )
        self.assertEqual(support_check["state"], "ready")
        self.assertEqual(readiness["findings"], [])

    def test_stale_authority_and_expected_state_are_explicitly_stale(self) -> None:
        cases = {
            "authority": lambda value: value.update(authority_revision="0" * 40),
            "registry": lambda value: value["request"]["expected_state"].update(
                registry_digest=UPSTREAM_DIGEST
            ),
        }
        for index, (name, mutate) in enumerate(cases.items(), start=2):
            with self.subTest(name=name):
                value = self.envelope()
                value["evaluation_id"] = f"prototype-landing-evaluation:sample-tool:{index}"
                mutate(value)
                response = self.post(self.rebind(value))
                self.assertEqual(response.status_code, 200, response.text)
                readiness = response.json()["readiness"]
                self.assertEqual(readiness["outcome"], "stale")
                check = next(
                    item for item in readiness["checks"] if item["id"] == "source-version-freshness"
                )
                self.assertEqual(check["state"], "stale")
                self.assertIn("source-state-stale", response.text)

    def test_identity_support_mutation_data_and_visibility_fail_closed(self) -> None:
        cases = {
            "support": self.envelope(row_state="unknown"),
            "mutation": self.envelope(mutation_set=("prototype-source",)),
            "data": self.envelope(data_mode="real-readonly"),
            "visibility": self.envelope(visibility="public-demo"),
        }
        for index, (name, value) in enumerate(cases.items(), start=10):
            with self.subTest(name=name):
                value["evaluation_id"] = f"prototype-landing-evaluation:sample-tool:{index}"
                response = self.post(self.bind(value, "evaluation_digest"))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["readiness"]["outcome"], "blocked")

        self.write_registry([{
            "id": "sample-tool",
            "name": "Existing Sample Tool",
            "lifecycle": "exploring",
            "owner": "Workspace Prototype Studio",
            "visibility_tier": "private-internal",
            "data_mode": "synthetic",
            "mutation_boundary": "none",
            "paths": {},
        }])
        self.commit("Add identity collision")
        value = self.envelope()
        value["evaluation_id"] = "prototype-landing-evaluation:sample-tool:19"
        response = self.post(self.bind(value, "evaluation_digest"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["readiness"]["outcome"], "blocked")

    def test_external_import_and_security_trigger_dependencies_are_blocked(self) -> None:
        cases = (
            self.envelope(
                posture="reference-dedicated-owner-source",
                source_ref="repo://dedicated-owner/sample-tool",
                source_revision="a" * 40,
                mutation_set=("registry-record", "prototype-docs", "validation-plan"),
            ),
            self.envelope(
                posture="import-to-studio",
                source_ref="import://operator/sample-tool",
                origin_digest=UPSTREAM_DIGEST,
                imported_digest="sha256:" + "2" * 64,
            ),
            self.envelope(
                constraints=[{"code": "credential-use", "detail": "Needs a credential."}]
            ),
        )
        for index, value in enumerate(cases, start=20):
            with self.subTest(posture=value["request"]["source_plan"]["posture"]):
                value["evaluation_id"] = f"prototype-landing-evaluation:sample-tool:{index}"
                response = self.post(self.bind(value, "evaluation_digest"))
                self.assertEqual(response.status_code, 200, response.text)
                readiness = response.json()["readiness"]
                self.assertEqual(readiness["outcome"], "blocked")
                self.assertTrue(readiness["security_trigger_refs"])

    def test_api_auth_conflict_caller_isolation_expiry_and_storage_failure(self) -> None:
        value = self.envelope()
        path = "/v1/readiness/prototype-landing"
        self.assertEqual(self.client.post(path, json=value).status_code, 401)
        self.assertEqual(
            self.client.post(path, content=b"x" * (256 * 1024 + 1), headers=self.headers).status_code,
            413,
        )
        response = self.post(value)
        self.assertEqual(response.status_code, 200, response.text)
        readiness = response.json()["readiness"]
        conflict = copy.deepcopy(value)
        conflict["execution_ref"] = "execution:changed"
        conflict = self.bind(conflict, "evaluation_digest")
        self.assertEqual(self.post(conflict).status_code, 409)
        with self.assertRaises(PrototypeLandingConflict):
            self.service.issue(canonical_bytes(value), actor="other-caller")
        token = readiness["readiness_digest"].removeprefix("sha256:")
        other_headers = {
            "x-wgcf-caller-id": "workspace-governance-control-fabric",
            "x-wgcf-caller-secret": "r" * 32,
        }
        self.assertEqual(
            self.client.get(
                f"/v1/readiness/prototype-landing/{token}", headers=other_headers
            ).status_code,
            404,
        )
        expired = self.new_service(clock=lambda: FIXED_TIME + timedelta(minutes=16))
        self.assertEqual(
            expired.read(token, actor="operator-orchestration-service")["ledger"]["state"],
            "expired",
        )
        metadata.drop_all(self.engine)
        fresh = self.envelope()
        fresh["evaluation_id"] = "prototype-landing-evaluation:sample-tool:99"
        self.assertEqual(self.post(self.bind(fresh, "evaluation_digest")).status_code, 503)

    def test_contract_or_source_dependency_failure_is_unavailable(self) -> None:
        contract = self.repo / "contracts/prototype-landing/prototype-landing.yaml"
        contract.write_text(contract.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        self.commit("Tamper source contract")
        value = self.envelope()
        self.assertEqual(self.post(value).status_code, 503)

    def test_source_activation_is_pinned_to_normal_availability_review(self) -> None:
        contracts = PrototypeLandingContracts.load()
        self.assertTrue(contracts.manifest["runtime_activation"])
        self.assertEqual(
            contracts.manifest["activation_review"],
            {
                "repo": "security-architecture",
                "commit": "7acfd9f86c24e8d454c7df8ee29abfbf2ad8ae20",
                "path": (
                    "docs/reviews/components/"
                    "2026-09-08-prototype-landing-normal-availability.md"
                ),
                "content_sha256": (
                    "80091cb2ac0154711ed011edd832d07ef"
                    "540ad7e9732e9eae2c68b9c237d076f"
                ),
                "decision": "approved-with-findings",
            },
        )

    def test_runtime_still_requires_profile_and_explicit_environment_activation(self) -> None:
        with patch.dict(
            "os.environ",
            {"WGCF_RUNTIME_PROFILE": "dev-integration"},
            clear=True,
        ):
            with self.assertRaises(PrototypeLandingUnavailable):
                build_prototype_landing_readiness_runtime()
        with patch.dict(
            "os.environ",
            {
                "WGCF_RUNTIME_PROFILE": "stage",
                "WGCF_PROTOTYPE_LANDING_READINESS_ENABLED": "true",
            },
            clear=True,
        ):
            with self.assertRaises(PrototypeLandingUnavailable):
                build_prototype_landing_readiness_runtime()
