from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timezone
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
from control_fabric_core.db.models import (
    LedgerEvent,
    WorkspaceInventoryEvaluationRecord,
    metadata,
)
from control_fabric_core.workspace_inventory_contracts import (
    InventoryAuthority,
    InventoryContracts,
    InventoryUnavailable,
    artifact_digest,
    canonical_bytes,
    digest,
)
from control_fabric_core.workspace_inventory_readiness import (
    InventoryConflict,
    WorkspaceInventoryReadinessService,
    build_workspace_inventory_readiness_runtime,
)
from wgcf_api.app import create_app


class WorkspaceInventoryReadinessTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "workspace-governance"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Inventory Test")
        self.git("config", "user.email", "inventory@example.invalid")
        contracts = InventoryContracts.load()
        for path, raw in contracts.files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)

        self.register = {
            "schema_version": 2,
            "repos": {"candidate-repo": self.intake_entry("repo", "candidate-repo")},
            "products": {"candidate-product": self.intake_entry("product", "candidate-product")},
            "components": {
                "candidate-component": self.intake_entry("component", "candidate-component")
            },
        }
        self.inventories = {
            "repos": {
                "schema_version": 2,
                "repos": {"anchor-repo": self.active_value("repo", "anchor-repo", migrated=True)},
                "retired_repos": {},
            },
            "products": {
                "schema_version": 2,
                "products": {
                    "anchor-product": self.active_value("product", "anchor-product", migrated=True)
                },
            },
            "components": {
                "schema_version": 2,
                "components": {
                    "anchor-component": self.active_value(
                        "component", "anchor-component", migrated=True
                    )
                },
            },
        }
        self.write("intake-register", self.register)
        for name, value in self.inventories.items():
            self.write(name, value)
        self.commit()
        self.pin = self.git("rev-parse", "HEAD")
        self.contracts = replace(
            contracts,
            manifest={**contracts.manifest, "authority_commit": self.pin},
        )
        self.authority = InventoryAuthority(
            self.repo,
            self.contracts,
            trusted_ref="refs/heads/main",
        )
        self.engine = create_engine(f"sqlite:///{self.root / 'ledger.sqlite'}")
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.service = self.new_service()
        self.client = TestClient(
            create_app(
                workspace_inventory_readiness=self.service,
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

    def new_service(self) -> WorkspaceInventoryReadinessService:
        return WorkspaceInventoryReadinessService(
            session_factory=self.sessions,
            authority=self.authority,
            service_identity_ref="spiffe://test/wgcf",
            implementation_ref="1" * 40,
            clock=lambda: datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc),
        )

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def write(self, name: str, data: dict) -> None:
        path = self.repo / "contracts" / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    def commit(self) -> None:
        self.git("add", ".")
        self.git("commit", "-m", "Test committed authority")

    @staticmethod
    def validation_behavior() -> dict:
        return {
            "posture": "required",
            "wgcf_graph_role": "workspace-component",
            "catalog_refs": ["component:workspace-governance"],
            "notes": "Validated by the test authority.",
        }

    def intake_entry(self, kind: str, name: str, *, status: str = "admitted") -> dict:
        specific = {
            "repo": {
                "repo_class": "product",
                "requires_security_bindings": True,
                "security_owner": "security-architecture",
            },
            "product": {
                "platform_owner": "platform-engineering",
                "security_owner": "security-architecture",
                "runtime_owner": "runtime-owner",
                "source_owners": ["source-owner"],
                "intended_endpoint": "loopback",
            },
            "component": {
                "component_class": "shared-platform",
                "owner_repo": "workspace-governance-control-fabric",
                "security_owner": "security-architecture",
                "product": None,
            },
        }[kind]
        source_digest = digest({"kind": kind, "name": name})
        return {
            "status": status,
            "decision_source": "operator",
            "owner_route": "workspace-governance",
            "record": {
                "id": f"{kind}:{name}",
                "version": 1,
                "source": {"class": "direct", "ref": f"source:{name}", "digest": source_digest},
                "decision": {
                    "id": f"decision:{name}",
                    "ref": f"decision:{name}",
                    "digest": source_digest,
                    "source": "operator",
                    "operator_ref": "operator:test",
                    "decided_at": "2026-09-05T10:00:00Z",
                },
                "last_mutation": {
                    "id": f"intake-mutation:{name}",
                    "idempotency_key": f"intake:{name}",
                    "request_ref": f"intake-request:{name}",
                    "request_digest": source_digest,
                    "decision_ref": f"decision:{name}",
                    "decision_digest": source_digest,
                    "applied_at": "2026-09-05T10:01:00Z",
                },
            },
            **specific,
            "validation_behavior": self.validation_behavior(),
            "notes": "Admitted test entrant.",
        }

    def active_value(self, kind: str, name: str, *, migrated: bool = False) -> dict:
        specific = {
            "repo": {
                "posture": "active",
                "lifecycle": "active",
                "repo_class": "product",
                "requires_security_bindings": True,
                "owns": ["test source"],
                "must_not_own": ["workspace policy"],
                "allowed_authoritative_refs": ["README.md"],
            },
            "product": {
                "posture": "active",
                "maturity": "owner-managed",
                "lifecycle": "owner-managed",
                "platform_owner": "platform-engineering",
                "security_owner": "security-architecture",
                "runtime_owner": "runtime-owner",
                "source_owners": ["source-owner"],
                "stage_supported": False,
                "governed_prod_promotion": False,
                "highest_real_endpoint": "loopback",
            },
            "component": {
                "posture": "active",
                "lifecycle": "active",
                "component_class": "shared-platform",
                "owner_repo": "workspace-governance-control-fabric",
                "product": None,
                "security_owner": "security-architecture",
            },
        }[kind]
        if not migrated:
            return {**specific, "validation_behavior": self.validation_behavior()}
        source_digest = digest({"legacy": name})
        return {
            "record": {
                "id": f"{kind}:{name}",
                "version": 1,
                "lineage": {
                    "source": "legacy-migration",
                    "source_ref": f"git:test#{name}",
                    "source_digest": source_digest,
                    "intake_entry_version": None,
                },
                "last_mutation": {
                    "id": f"migration:{name}",
                    "action": "migrate",
                    "idempotency_key": f"migration:{name}",
                    "request_ref": None,
                    "request_digest": None,
                    "readiness_ref": None,
                    "readiness_digest": None,
                    "applied_at": "2026-09-05T09:00:00Z",
                },
            },
            **specific,
            "validation_behavior": self.validation_behavior(),
        }

    def envelope(self, kind: str = "component") -> dict:
        name = f"candidate-{kind}"
        collection = f"{kind}s" if kind != "repo" else "repos"
        entry = self.register[collection][name]
        inventory = self.inventories[collection]
        target = {"kind": kind, "name": name, "record_id": f"{kind}:{name}"}
        request = {
            "schema_version": 1,
            "artifact_type": "workspace-inventory-promotion-request",
            "request_id": f"inventory-request:{kind}",
            "requested_at": "2026-09-06T09:00:00Z",
            "operator_ref": "operator:test",
            "correlation_ref": "delivery:890",
            "idempotency_key": f"inventory-promotion:{kind}",
            "target": target,
            "intake_entry_ref": {
                "id": entry["record"]["id"],
                "version": entry["record"]["version"],
                "digest": digest(entry),
            },
            "expected_state": {
                "intake_register_digest": digest(self.register),
                "active_inventory_digest": digest(inventory),
                "intake_entry_version": entry["record"]["version"],
                "intake_entry_digest": digest(entry),
                "active_record_version": None,
                "active_record_digest": None,
            },
            "active_record": {
                "kind": kind,
                "id": target["record_id"],
                "value": self.active_value(kind, name),
            },
            "approval_refs": ["approval:operator:test"],
        }
        request["request_digest"] = artifact_digest(request, "request_digest")
        envelope = {
            "schema_version": 1,
            "artifact_type": "wgcf-workspace-inventory-readiness-evaluation",
            "evaluation_id": f"inventory-evaluation:{kind}",
            "session_ref": "session:test",
            "execution_ref": "execution:test",
            "authority_revision": self.git("rev-parse", "HEAD"),
            "request": request,
        }
        return self.bind(envelope)

    @staticmethod
    def bind(value: dict) -> dict:
        request = value["request"]
        request["request_digest"] = artifact_digest(request, "request_digest")
        value["evaluation_digest"] = artifact_digest(value, "evaluation_digest")
        return value

    def post(self, value: dict):
        return self.client.post(
            "/v1/readiness/workspace-inventory",
            content=canonical_bytes(value),
            headers=self.headers,
        )

    def test_all_kinds_are_ready_replayable_readable_and_non_mutating(self) -> None:
        before = self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")
        for kind in ("repo", "product", "component"):
            with self.subTest(kind=kind):
                value = self.envelope(kind)
                response = self.post(value)
                self.assertEqual(response.status_code, 200, response.text)
                readiness = response.json()["readiness"]
                self.assertEqual(readiness["outcome"], "ready", readiness["findings"])
                self.assertEqual(
                    readiness["policy_ref"]["id"],
                    f"workspace-active-inventory@{self.pin}",
                )
                self.assertEqual(
                    readiness["readiness_digest"],
                    artifact_digest(readiness, "readiness_digest"),
                )
                replay = self.new_service().issue(
                    canonical_bytes(value), actor="operator-orchestration-service"
                )
                self.assertEqual(replay["ledger"]["resolution"], "reused")
                token = readiness["readiness_digest"].removeprefix("sha256:")
                read = self.client.get(
                    f"/v1/readiness/workspace-inventory/{token}", headers=self.headers
                )
                self.assertEqual(read.json()["readiness"], readiness)
        self.assertEqual(
            (self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")), before
        )
        with self.sessions() as session:
            self.assertEqual(
                len(session.scalars(select(WorkspaceInventoryEvaluationRecord)).all()), 3
            )
            self.assertEqual(len(session.scalars(select(LedgerEvent)).all()), 9)

    def test_blocked_policy_matrix(self) -> None:
        cases = {
            "intake-entry-not-admitted": lambda v: self.register["components"][
                "candidate-component"
            ].update(status="proposed"),
            "active-record-invalid": lambda v: v["request"]["active_record"]["value"].pop(
                "owner_repo"
            ),
            "compatibility-alias-mismatch": lambda v: v["request"]["active_record"][
                "value"
            ].update(lifecycle="suspended"),
            "active-record-target-mismatch": lambda v: v["request"]["active_record"].update(
                id="component:other"
            ),
            "idempotency-conflict": lambda v: v["request"].update(
                idempotency_key="migration:anchor-component"
            ),
            "future-request": lambda v: v["request"].update(
                requested_at="2026-09-07T10:00:00Z"
            ),
        }
        for code, mutate in cases.items():
            with self.subTest(code=code):
                value = self.envelope()
                value["evaluation_id"] = f"evaluation:{code}"
                mutate(value)
                if code == "intake-entry-not-admitted":
                    self.write("intake-register", self.register)
                    self.commit()
                    value = self.envelope()
                    value["evaluation_id"] = f"evaluation:{code}"
                self.bind(value)
                response = self.post(value)
                self.assertEqual(response.status_code, 200, response.text)
                readiness = response.json()["readiness"]
                self.assertEqual(readiness["outcome"], "blocked")
                self.assertIn(code, [finding["code"] for finding in readiness["findings"]])
            if code == "intake-entry-not-admitted":
                self.register["components"]["candidate-component"]["status"] = "admitted"
                self.write("intake-register", self.register)
                self.commit()

    def test_stale_authority_and_source_bindings(self) -> None:
        for code, mutate in (
            ("stale-authority", lambda value: value.update(authority_revision="0" * 40)),
            (
                "stale-inventory-state",
                lambda value: value["request"]["expected_state"].update(
                    active_inventory_digest=digest("stale")
                ),
            ),
            (
                "stale-intake-entry",
                lambda value: value["request"]["intake_entry_ref"].update(version=2),
            ),
        ):
            value = self.envelope()
            value["evaluation_id"] = f"evaluation:{code}"
            mutate(value)
            self.bind(value)
            readiness = self.post(value).json()["readiness"]
            self.assertEqual(readiness["outcome"], "stale")
            self.assertIn(code, [finding["code"] for finding in readiness["findings"]])

    def test_committed_authority_ignores_dirty_state_and_contract_drift_fails_closed(self) -> None:
        value = self.envelope()
        path = self.repo / "contracts" / "intake-register.yaml"
        path.write_text("invalid: dirty state is not authority\n", encoding="utf-8")
        self.assertEqual(self.post(value).json()["readiness"]["outcome"], "ready")
        self.git("restore", "contracts/intake-register.yaml")
        contract = self.repo / "contracts" / "workspace-active-inventory.yaml"
        contract.write_text(contract.read_text() + "\n# changed\n", encoding="utf-8")
        self.commit()
        value = self.envelope()
        value["evaluation_id"] = "evaluation:contract-drift"
        self.bind(value)
        self.assertEqual(self.post(value).status_code, 503)

    def test_api_auth_digest_identity_size_and_missing_target(self) -> None:
        value = self.envelope()
        path = "/v1/readiness/workspace-inventory"
        self.assertEqual(self.client.post(path, json=value).status_code, 401)
        bad = copy.deepcopy(value)
        bad["request"]["operator_ref"] = "changed"
        self.assertEqual(self.post(bad).status_code, 422)
        self.assertEqual(
            self.client.post(path, content=b"x" * 65537, headers=self.headers).status_code,
            413,
        )
        self.assertEqual(self.post(value).status_code, 200)
        conflict = copy.deepcopy(value)
        conflict["execution_ref"] = "execution:other"
        self.assertEqual(self.post(self.bind(conflict)).status_code, 409)
        with self.assertRaises(InventoryConflict):
            self.service.issue(canonical_bytes(value), actor="other-caller")

        self.register["components"].pop("candidate-component")
        self.write("intake-register", self.register)
        self.commit()
        missing = self.envelope("repo")
        missing["evaluation_id"] = "evaluation:missing"
        missing["request"]["target"] = {
            "kind": "component",
            "name": "candidate-component",
            "record_id": "component:candidate-component",
        }
        missing["request"]["active_record"] = {
            "kind": "component",
            "id": "component:candidate-component",
            "value": self.active_value("component", "candidate-component"),
        }
        self.bind(missing)
        self.assertEqual(self.post(missing).status_code, 422)

    def test_existing_active_identity_is_rejected_as_wrong_operation(self) -> None:
        self.inventories["components"]["components"]["candidate-component"] = self.active_value(
            "component", "candidate-component", migrated=True
        )
        self.write("components", self.inventories["components"])
        self.commit()
        value = self.envelope()
        value["evaluation_id"] = "evaluation:already-active"
        self.bind(value)
        response = self.post(value)
        self.assertEqual(response.status_code, 422)
        self.assertIn("use a lifecycle operation", response.text)

    def test_caller_isolation_tamper_and_storage_failure(self) -> None:
        value = self.envelope()
        readiness = self.post(value).json()["readiness"]
        token = readiness["readiness_digest"].removeprefix("sha256:")
        other = {
            "x-wgcf-caller-id": "workspace-governance-control-fabric",
            "x-wgcf-caller-secret": "r" * 32,
        }
        self.assertEqual(
            self.client.get(f"/v1/readiness/workspace-inventory/{token}", headers=other).status_code,
            404,
        )
        with self.sessions.begin() as session:
            row = session.get(WorkspaceInventoryEvaluationRecord, value["evaluation_id"])
            row.readiness = {**row.readiness, "outcome": "blocked"}
        self.assertEqual(
            self.client.get(
                f"/v1/readiness/workspace-inventory/{token}", headers=self.headers
            ).status_code,
            503,
        )
        metadata.drop_all(self.engine)
        self.assertEqual(self.post(self.envelope("product")).status_code, 503)

    def test_default_runtime_activation_is_denied(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "WGCF_RUNTIME_PROFILE": "dev-integration",
                "WGCF_WORKSPACE_INVENTORY_READINESS_ENABLED": "true",
            },
        ):
            with self.assertRaises(InventoryUnavailable):
                build_workspace_inventory_readiness_runtime()
