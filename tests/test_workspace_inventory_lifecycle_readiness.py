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
    WorkspaceInventoryLifecycleEvaluationRecord,
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
from control_fabric_core.workspace_inventory_lifecycle_readiness import (
    InventoryLifecycleConflict,
    WorkspaceInventoryLifecycleReadinessService,
    build_workspace_inventory_lifecycle_readiness_runtime,
)
from wgcf_api.app import create_app


class WorkspaceInventoryLifecycleReadinessTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "workspace-governance"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Lifecycle Test")
        self.git("config", "user.email", "lifecycle@example.invalid")
        contracts = InventoryContracts.load()
        for path, raw in contracts.files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)

        self.record = self.active_component()
        self.inventories = {
            "repos": {
                "schema_version": 2,
                "repos": {"anchor-repo": self.active_repo()},
                "retired_repos": {},
            },
            "products": {
                "schema_version": 2,
                "products": {"anchor-product": self.active_product()},
            },
            "components": {
                "schema_version": 2,
                "components": {"lifecycle-target": self.record},
            },
        }
        self.history = {"schema_version": 1, "events": []}
        self.write("intake-register", {"schema_version": 2, "repos": {}, "products": {}, "components": {}})
        for name, value in self.inventories.items():
            self.write(name, value)
        self.write("workspace-inventory-history", self.history)
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
                workspace_inventory_lifecycle_readiness=self.service,
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

    def new_service(self) -> WorkspaceInventoryLifecycleReadinessService:
        return WorkspaceInventoryLifecycleReadinessService(
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
        self.git("commit", "-m", "Test committed lifecycle authority")

    @staticmethod
    def validation_behavior() -> dict:
        return {
            "posture": "required",
            "wgcf_graph_role": "workspace-component",
            "catalog_refs": ["component:workspace-governance"],
            "notes": "Validated by the lifecycle test authority.",
        }

    def record_envelope(self, kind: str, name: str) -> dict:
        source_digest = digest({"legacy": name})
        return {
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
        }

    def active_component(self) -> dict:
        return {
            "record": self.record_envelope("component", "lifecycle-target"),
            "posture": "active",
            "lifecycle": "active",
            "component_class": "shared-platform",
            "owner_repo": "workspace-governance-control-fabric",
            "product": None,
            "security_owner": "security-architecture",
            "validation_behavior": self.validation_behavior(),
        }

    def active_repo(self) -> dict:
        return {
            "record": self.record_envelope("repo", "anchor-repo"),
            "posture": "active",
            "lifecycle": "active",
            "repo_class": "product",
            "requires_security_bindings": True,
            "owns": ["test source"],
            "must_not_own": ["workspace policy"],
            "allowed_authoritative_refs": ["README.md"],
            "validation_behavior": self.validation_behavior(),
        }

    def active_product(self) -> dict:
        return {
            "record": self.record_envelope("product", "anchor-product"),
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
            "validation_behavior": self.validation_behavior(),
        }

    def envelope(self, action: str = "suspend") -> dict:
        record = self.inventories["components"]["components"]["lifecycle-target"]
        request = {
            "schema_version": 1,
            "artifact_type": "workspace-inventory-lifecycle-request",
            "request_id": f"request:lifecycle:{action}",
            "requested_at": "2026-09-06T09:00:00Z",
            "operator_ref": "operator:test",
            "correlation_ref": "delivery:890",
            "idempotency_key": f"lifecycle:component:lifecycle-target:{action}",
            "action": action,
            "target": {
                "kind": "component",
                "name": "lifecycle-target",
                "record_id": "component:lifecycle-target",
            },
            "expected_state": {
                "active_inventory_digest": digest(self.inventories["components"]),
                "history_digest": digest(self.history),
                "record_version": record["record"]["version"],
                "record_digest": digest(record),
                "posture": record["posture"],
            },
            "requested_value": None,
            "prior_event_ref": None,
            "reason": f"Exercise {action} readiness.",
            "impact_acknowledgements": ["impact:reviewed"],
            "approval_refs": ["approval:operator:test"],
        }
        if action == "update":
            request["requested_value"] = copy.deepcopy(record)
            request["requested_value"].pop("record")
            request["requested_value"]["owner_repo"] = "updated-owner"
        if action == "restore" and self.history["events"]:
            latest = self.history["events"][-1]
            request["prior_event_ref"] = {
                "id": latest["event_id"],
                "digest": latest["event_digest"],
            }
        request["request_digest"] = artifact_digest(request, "request_digest")
        envelope = {
            "schema_version": 1,
            "artifact_type": "wgcf-workspace-inventory-lifecycle-evaluation",
            "evaluation_id": f"evaluation:lifecycle:{action}",
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
            "/v1/readiness/workspace-inventory-lifecycle",
            content=canonical_bytes(value),
            headers=self.headers,
        )

    def install_history_state(self, *, action: str, posture: str) -> None:
        before = copy.deepcopy(self.record)
        after = copy.deepcopy(before)
        after["posture"] = posture
        after["lifecycle"] = posture
        after["record"]["version"] = 2
        after["record"]["last_mutation"] = {
            "id": f"workspace-inventory-lifecycle:test-{action}",
            "action": action,
            "idempotency_key": f"prior:{action}",
            "request_ref": f"prior-request:{action}",
            "request_digest": digest({"prior": action}),
            "readiness_ref": f"prior-readiness:{action}",
            "readiness_digest": digest({"prior-readiness": action}),
            "applied_at": "2026-09-06T08:00:00Z",
        }
        event = {
            "event_id": f"workspace-inventory-event:component:lifecycle-target:2",
            "sequence": 1,
            "target": {
                "kind": "component",
                "name": "lifecycle-target",
                "record_id": "component:lifecycle-target",
            },
            "action": action,
            "idempotency_key": f"prior:{action}",
            "before": {
                "record_version": 1,
                "record_digest": digest(before),
                "posture": before["posture"],
            },
            "after": {
                "record_version": 2,
                "record_digest": digest(after),
                "posture": posture,
            },
            "request_ref": {
                "id": f"prior-request:{action}",
                "digest": digest({"prior": action}),
            },
            "readiness_ref": {
                "id": f"prior-readiness:{action}",
                "digest": digest({"prior-readiness": action}),
            },
            "previous_event_ref": None,
            "operator_ref": "operator:test",
            "applied_at": "2026-09-06T08:00:00Z",
        }
        event["event_digest"] = digest(event)
        self.record = after
        self.inventories["components"]["components"]["lifecycle-target"] = after
        self.history = {"schema_version": 1, "events": [event]}
        self.write("components", self.inventories["components"])
        self.write("workspace-inventory-history", self.history)
        self.commit()

    def test_ready_actions_replay_readback_and_non_mutation(self) -> None:
        before = self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")
        for action in ("update", "suspend", "retire"):
            with self.subTest(action=action):
                value = self.envelope(action)
                response = self.post(value)
                self.assertEqual(response.status_code, 200, response.text)
                readiness = response.json()["readiness"]
                self.assertEqual(readiness["outcome"], "ready")
                self.assertEqual(readiness["findings"], [])
                self.assertEqual(readiness["action"], action)
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
                    f"/v1/readiness/workspace-inventory-lifecycle/{token}",
                    headers=self.headers,
                )
                self.assertEqual(read.json()["readiness"], readiness)
        self.assertEqual(
            (self.git("status", "--porcelain"), self.git("rev-parse", "HEAD")),
            before,
        )
        with self.sessions() as session:
            self.assertEqual(
                len(session.scalars(select(WorkspaceInventoryLifecycleEvaluationRecord)).all()),
                3,
            )
            self.assertEqual(len(session.scalars(select(LedgerEvent)).all()), 9)

    def test_restore_is_ready_only_from_latest_suspension_or_retirement(self) -> None:
        self.install_history_state(action="suspend", posture="suspended")
        response = self.post(self.envelope("restore"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["readiness"]["outcome"], "ready")

    def test_blocked_policy_matrix_is_deterministic(self) -> None:
        cases = {
            "stale-authority": lambda value: value.update(authority_revision="0" * 40),
            "stale-inventory-state": lambda value: value["request"]["expected_state"].update(
                history_digest=digest("stale")
            ),
            "illegal-lifecycle-transition": lambda value: value["request"].update(action="restore"),
            "future-request": lambda value: value["request"].update(
                requested_at="2026-09-07T10:00:00Z"
            ),
            "posture-change-through-update": lambda value: value["request"][
                "requested_value"
            ].update(posture="suspended"),
        }
        for code, mutate in cases.items():
            with self.subTest(code=code):
                value = self.envelope("update" if code == "posture-change-through-update" else "suspend")
                value["evaluation_id"] = f"evaluation:{code}"
                mutate(value)
                if code == "illegal-lifecycle-transition":
                    value["request"]["prior_event_ref"] = {
                        "id": "missing",
                        "digest": digest("missing"),
                    }
                self.bind(value)
                response = self.post(value)
                self.assertEqual(response.status_code, 200, response.text)
                readiness = response.json()["readiness"]
                self.assertEqual(readiness["outcome"], "blocked")
                self.assertTrue(any(f"[{code}]" in finding for finding in readiness["findings"]))

    def test_history_conflicts_and_restore_binding_are_denied(self) -> None:
        self.install_history_state(action="retire", posture="retired")
        stale = self.envelope("restore")
        stale["evaluation_id"] = "evaluation:stale-restore"
        stale["request"]["prior_event_ref"] = {
            "id": "other-event",
            "digest": digest("other-event"),
        }
        response = self.post(self.bind(stale))
        self.assertEqual(response.json()["readiness"]["outcome"], "blocked")
        self.assertIn("[stale-restore-event]", response.text)

        conflict = self.envelope("restore")
        conflict["evaluation_id"] = "evaluation:history-conflict"
        conflict["request"]["idempotency_key"] = "prior:retire"
        response = self.post(self.bind(conflict))
        self.assertEqual(response.json()["readiness"]["outcome"], "blocked")
        self.assertIn("[idempotency-conflict]", response.text)

    def test_api_auth_bounds_identity_and_caller_isolation(self) -> None:
        value = self.envelope()
        path = "/v1/readiness/workspace-inventory-lifecycle"
        self.assertEqual(self.client.post(path, json=value).status_code, 401)
        bad = copy.deepcopy(value)
        bad["request"]["reason"] = "changed"
        self.assertEqual(self.post(bad).status_code, 422)
        self.assertEqual(
            self.client.post(path, content=b"x" * 65537, headers=self.headers).status_code,
            413,
        )
        readiness = self.post(value).json()["readiness"]
        conflict = copy.deepcopy(value)
        conflict["execution_ref"] = "execution:other"
        self.assertEqual(self.post(self.bind(conflict)).status_code, 409)
        with self.assertRaises(InventoryLifecycleConflict):
            self.service.issue(canonical_bytes(value), actor="other-caller")
        token = readiness["readiness_digest"].removeprefix("sha256:")
        other = {
            "x-wgcf-caller-id": "workspace-governance-control-fabric",
            "x-wgcf-caller-secret": "r" * 32,
        }
        self.assertEqual(
            self.client.get(
                f"/v1/readiness/workspace-inventory-lifecycle/{token}",
                headers=other,
            ).status_code,
            404,
        )

    def test_invalid_history_and_storage_failure_fail_closed(self) -> None:
        event = {
            "event_id": "bad-event",
            "event_digest": digest("wrong"),
            "sequence": 1,
            "target": {"kind": "component", "name": "lifecycle-target", "record_id": "component:lifecycle-target"},
            "action": "suspend",
            "idempotency_key": "bad-event",
            "before": {"record_version": 1, "record_digest": digest(self.record), "posture": "active"},
            "after": {"record_version": 1, "record_digest": digest(self.record), "posture": "active"},
            "request_ref": {"id": "bad-request", "digest": digest("bad-request")},
            "readiness_ref": {"id": "bad-readiness", "digest": digest("bad-readiness")},
            "previous_event_ref": None,
            "operator_ref": "operator:test",
            "applied_at": "2026-09-06T08:00:00Z",
        }
        self.history["events"] = [event]
        self.write("workspace-inventory-history", self.history)
        self.commit()
        value = self.envelope()
        self.assertEqual(self.post(value).status_code, 503)

        metadata.drop_all(self.engine)
        self.assertEqual(self.post(self.envelope("retire")).status_code, 503)

    def test_default_runtime_activation_is_denied(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "WGCF_RUNTIME_PROFILE": "dev-integration",
                "WGCF_WORKSPACE_INVENTORY_LIFECYCLE_READINESS_ENABLED": "true",
            },
        ):
            with self.assertRaises(InventoryUnavailable):
                build_workspace_inventory_lifecycle_readiness_runtime()
