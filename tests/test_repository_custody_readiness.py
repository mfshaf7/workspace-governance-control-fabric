from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from control_fabric_core.canonical_json import canonical_digest, canonical_json_bytes
from control_fabric_core.db import metadata
from control_fabric_core.db.models import LedgerEvent, RepositoryCustodyDecisionRecord
from control_fabric_core.repository_custody_contracts import (
    RepositoryCustodyContractBundle,
    RepositoryCustodyContractError,
)
from control_fabric_core.repository_custody_readiness import (
    RepositoryCustodyReadinessConflict,
    RepositoryCustodyReadinessRequestError,
    RepositoryCustodyReadinessService,
    RepositoryCustodyReadinessUnavailable,
    build_repository_custody_readiness_runtime,
)


IMPLEMENTATION_REF = "f" * 40
SERVICE_IDENTITY_REF = (
    "kubernetes://devint-governance-control-fabric/"
    "serviceaccount/workspace-governance-control-fabric-api"
)


class RepositoryCustodyReadinessTests(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.contracts = RepositoryCustodyContractBundle.load()
        self.service = RepositoryCustodyReadinessService(
            session_factory=self.sessions,
            service_identity_ref=SERVICE_IDENTITY_REF,
            implementation_ref=IMPLEMENTATION_REF,
            contract_bundle=self.contracts,
            clock=lambda: datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc),
        )

    def tearDown(self) -> None:
        self.engine.dispose()

    def request(self, **overrides: object) -> bytes:
        payload = {
            "schema_version": 1,
            "artifact_type": "repository_custody_request",
            "request_id": "repository-custody-request:link-example-001",
            "requested_at": "2026-08-29T07:59:00Z",
            "action": "link-existing",
            "operator_ref": {
                "uri": "https://workspace-governance.local/operators/example",
                "digest": "sha256:" + "1" * 64,
            },
            "workflow": {
                "workflow_id": "repository-custody",
                "workflow_version": "1",
                "execution_id": "repository-custody-link-example-001",
            },
            "target": {
                "provider": "github",
                "provider_host": "github.com",
                "owner": "example-owner",
                "name": "example-repository",
                "provider_repository_id": "123456789",
            },
            "requested_custody": {
                "workspace_owner_ref": "repo:example-repository",
                "custody_kind": "dedicated-owner-repo",
            },
            "authority": {
                "policy_profile_ref": {
                    "uri": self.contracts.authority_uri,
                    "digest": self.contracts.authority_digest,
                },
                "approval_ref": {
                    "uri": (
                        "https://workspace-governance.local/approvals/"
                        "repository-custody/link-example-001"
                    ),
                    "digest": "sha256:" + "8" * 64,
                },
                "credential_binding_ref": {
                    "uri": (
                        "https://platform-engineering.local/credential-bindings/"
                        "github-app/repository-read"
                    ),
                    "digest": "sha256:" + "3" * 64,
                },
            },
            "correlation": {
                "correlation_id": "repository-custody-link-example-001",
                "causation_id": None,
            },
            "idempotency_key": "repository-custody-link-example-001",
            **overrides,
        }
        payload["request_digest"] = canonical_digest(payload)
        return canonical_json_bytes(payload)

    def provision_request(self, **overrides: object) -> bytes:
        payload = json.loads(self.request())
        payload.update(
            {
                "request_id": "repository-custody-request:provision-example-001",
                "action": "provision-new",
                "workflow": {
                    "workflow_id": "repository-custody",
                    "workflow_version": "1",
                    "execution_id": "repository-custody-provision-example-001",
                },
                "target": {
                    "provider": "github",
                    "provider_host": "github.com",
                    "owner": "example-organization",
                    "owner_scope": "organization",
                    "name": "example-repository",
                    "provider_repository_id": None,
                },
                "provisioning": {
                    "description": "Example governed repository.",
                    "visibility": "private",
                    "initialize_with_readme": True,
                    "features": {
                        "issues": True,
                        "projects": False,
                        "wiki": False,
                        "discussions": False,
                    },
                    "merge_policy": {
                        "allow_squash_merge": True,
                        "allow_merge_commit": False,
                        "allow_rebase_merge": False,
                        "delete_branch_on_merge": True,
                    },
                },
                "authority": {
                    **payload["authority"],
                    "approval_ref": {
                        "uri": (
                            "https://workspace-governance.local/approvals/"
                            "repository-custody/provision-example-001"
                        ),
                        "digest": "sha256:" + "8" * 64,
                    },
                    "credential_binding_ref": {
                        "uri": (
                            "https://platform-engineering.local/credential-bindings/"
                            "github-app/repository-create"
                        ),
                        "digest": "sha256:" + "3" * 64,
                    },
                },
                "correlation": {
                    "correlation_id": "repository-custody-provision-example-001",
                    "causation_id": None,
                },
                "idempotency_key": "repository-custody-provision-example-001",
            },
        )
        payload.update(overrides)
        payload.pop("request_digest", None)
        payload["request_digest"] = canonical_digest(payload)
        return canonical_json_bytes(payload)

    def test_repository_custody_readiness_positive_conformance(self) -> None:
        created = self.service.issue(self.request(), actor="operator-orchestration-service")
        replay = self.service.issue(self.request(), actor="operator-orchestration-service")
        token = created.decision["decision_id"].rsplit(":", 1)[1]
        read = self.service.read(token, actor="workspace-governance-control-fabric")

        self.assertEqual("allowed", created.decision["outcome"])
        self.assertEqual("link-existing", created.decision["action"])
        self.assertEqual("read-provider", created.decision["next_action"])
        self.assertIsNone(created.decision["approved_provisioning"])
        self.assertEqual(
            {"provider": "github", "provider_repository_id": "123456789"},
            created.decision["resolved_identity"],
        )
        self.assertEqual("created", created.resolution)
        self.assertEqual("reused", replay.resolution)
        self.assertEqual("read", read.resolution)
        self.assertEqual(created.decision, replay.decision)
        self.assertEqual(created.decision, read.decision)
        with self.sessions() as session:
            self.assertEqual(
                1,
                len(session.scalars(select(RepositoryCustodyDecisionRecord)).all()),
            )
            actions = {event.action for event in session.scalars(select(LedgerEvent)).all()}
        self.assertEqual(
            {
                "repository-custody.readiness.persisted",
                "repository-custody.readiness.reused",
                "repository-custody.readiness.read",
            },
            actions,
        )

    def test_repository_custody_readiness_negative_conformance(self) -> None:
        request = json.loads(self.request())
        request["authority"]["policy_profile_ref"]["digest"] = "sha256:" + "9" * 64
        request.pop("request_digest")
        request["request_digest"] = canonical_digest(request)

        result = self.service.issue(
            canonical_json_bytes(request),
            actor="operator-orchestration-service",
        )

        self.assertEqual("denied", result.decision["outcome"])
        self.assertEqual("stop", result.decision["next_action"])
        self.assertEqual(
            ["custody-policy-binding-stale"],
            [finding["code"] for finding in result.decision["findings"]],
        )

    def test_repository_provisioning_positive_conformance(self) -> None:
        created = self.service.issue(
            self.provision_request(),
            actor="operator-orchestration-service",
        )
        replay = self.service.issue(
            self.provision_request(),
            actor="operator-orchestration-service",
        )
        token = created.decision["decision_id"].rsplit(":", 1)[1]
        read = self.service.read(token, actor="workspace-governance-control-fabric")

        self.assertEqual("allowed", created.decision["outcome"])
        self.assertEqual("provision-new", created.decision["action"])
        self.assertEqual("create-provider", created.decision["next_action"])
        self.assertIsNone(created.decision["resolved_identity"])
        self.assertEqual(
            {
                "provider": "github",
                "provider_host": "github.com",
                "owner": "example-organization",
                "owner_scope": "organization",
                "name": "example-repository",
                "settings": json.loads(self.provision_request())["provisioning"],
            },
            created.decision["approved_provisioning"],
        )
        self.assertEqual("created", created.resolution)
        self.assertEqual("reused", replay.resolution)
        self.assertEqual("read", read.resolution)
        self.assertEqual(created.decision, replay.decision)
        self.assertEqual(created.decision, read.decision)
        with self.sessions() as session:
            row = session.get(
                RepositoryCustodyDecisionRecord,
                created.decision["decision_id"],
            )
            assert row is not None
            self.assertEqual("provision-new", row.action)
            self.assertIsNone(row.provider_repository_id)

    def test_lifecycle_action_is_rejected_by_custody_request_schema(self) -> None:
        request = json.loads(self.request(action="transfer-custody"))
        request.pop("request_digest")
        request["request_digest"] = canonical_digest(request)

        with self.assertRaisesRegex(
            RepositoryCustodyReadinessRequestError,
            "transfer-custody",
        ):
            self.service.issue(
                canonical_json_bytes(request),
                actor="operator-orchestration-service",
            )

    def test_repository_provisioning_negative_conformance(self) -> None:
        personal = json.loads(self.provision_request())
        personal["target"]["owner_scope"] = "personal"
        personal.pop("request_digest")
        personal["request_digest"] = canonical_digest(personal)
        with self.assertRaisesRegex(
            RepositoryCustodyReadinessRequestError,
            "owner_scope",
        ):
            self.service.issue(
                canonical_json_bytes(personal),
                actor="operator-orchestration-service",
            )

        incomplete = json.loads(self.provision_request())
        incomplete["provisioning"]["merge_policy"].pop("allow_squash_merge")
        incomplete.pop("request_digest")
        incomplete["request_digest"] = canonical_digest(incomplete)
        with self.assertRaisesRegex(
            RepositoryCustodyReadinessRequestError,
            "allow_squash_merge",
        ):
            self.service.issue(
                canonical_json_bytes(incomplete),
                actor="operator-orchestration-service",
            )

        missing_approval = json.loads(self.provision_request())
        missing_approval["authority"]["approval_ref"] = None
        missing_approval.pop("request_digest")
        missing_approval["request_digest"] = canonical_digest(missing_approval)
        with self.assertRaisesRegex(
            RepositoryCustodyReadinessRequestError,
            "approval_ref",
        ):
            self.service.issue(
                canonical_json_bytes(missing_approval),
                actor="operator-orchestration-service",
            )

        unsupported_provider = json.loads(self.provision_request())
        unsupported_provider["target"]["provider"] = "gitlab"
        unsupported_provider["target"]["provider_host"] = "gitlab.com"
        unsupported_provider.pop("request_digest")
        unsupported_provider["request_digest"] = canonical_digest(unsupported_provider)
        result = self.service.issue(
            canonical_json_bytes(unsupported_provider),
            actor="operator-orchestration-service",
        )
        self.assertEqual("denied", result.decision["outcome"])
        self.assertEqual(
            "custody-provisioning-provider-not-active",
            result.decision["findings"][0]["code"],
        )
        self.assertIsNone(result.decision["approved_provisioning"])

    def test_repository_provisioning_secret_bearing_reference_is_denied(self) -> None:
        secret_bearing = json.loads(self.provision_request())
        secret_bearing["authority"]["credential_binding_ref"]["uri"] += "?token=hidden"
        secret_bearing.pop("request_digest")
        secret_bearing["request_digest"] = canonical_digest(secret_bearing)
        result = self.service.issue(
            canonical_json_bytes(secret_bearing),
            actor="operator-orchestration-service",
        )
        self.assertEqual("denied", result.decision["outcome"])
        self.assertEqual(
            "credential-reference-secret-bearing",
            result.decision["findings"][0]["code"],
        )

    def test_repository_provisioning_stale_policy_is_denied(self) -> None:
        stale = json.loads(self.provision_request())
        stale["authority"]["policy_profile_ref"]["digest"] = "sha256:" + "9" * 64
        stale.pop("request_digest")
        stale["request_digest"] = canonical_digest(stale)

        result = self.service.issue(
            canonical_json_bytes(stale),
            actor="operator-orchestration-service",
        )

        self.assertEqual("denied", result.decision["outcome"])
        self.assertEqual("stop", result.decision["next_action"])
        self.assertIsNone(result.decision["approved_provisioning"])
        self.assertEqual(
            ["custody-policy-binding-stale"],
            [finding["code"] for finding in result.decision["findings"]],
        )

    def test_request_id_replay_with_different_content_is_rejected(self) -> None:
        self.service.issue(self.request(), actor="operator-orchestration-service")
        changed = json.loads(self.request())
        changed["requested_custody"]["workspace_owner_ref"] = "repo:another-owner"
        changed.pop("request_digest")
        changed["request_digest"] = canonical_digest(changed)

        with self.assertRaises(RepositoryCustodyReadinessConflict):
            self.service.issue(
                canonical_json_bytes(changed),
                actor="operator-orchestration-service",
            )

    def test_malformed_or_secret_bearing_input_fails_closed(self) -> None:
        malformed = json.loads(self.request())
        malformed["request_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(
            RepositoryCustodyReadinessRequestError,
            "does not match canonical request content",
        ):
            self.service.issue(
                canonical_json_bytes(malformed),
                actor="operator-orchestration-service",
            )

        graph_node_id = json.loads(self.request())
        graph_node_id["target"]["provider_repository_id"] = "R_kgDOExample"
        graph_node_id.pop("request_digest")
        graph_node_id["request_digest"] = canonical_digest(graph_node_id)
        with self.assertRaisesRegex(
            RepositoryCustodyReadinessRequestError,
            "provider_repository_id",
        ):
            self.service.issue(
                canonical_json_bytes(graph_node_id),
                actor="operator-orchestration-service",
            )

        secret_bearing = json.loads(self.request())
        secret_bearing["authority"]["credential_binding_ref"]["uri"] += "?token=hidden"
        secret_bearing.pop("request_digest")
        secret_bearing["request_digest"] = canonical_digest(secret_bearing)
        result = self.service.issue(
            canonical_json_bytes(secret_bearing),
            actor="operator-orchestration-service",
        )
        self.assertEqual("denied", result.decision["outcome"])
        self.assertEqual(
            "credential-reference-secret-bearing",
            result.decision["findings"][0]["code"],
        )

    def test_tampered_persisted_decision_is_not_readable(self) -> None:
        created = self.service.issue(self.request(), actor="operator-orchestration-service")
        with self.sessions.begin() as session:
            row = session.get(
                RepositoryCustodyDecisionRecord,
                created.decision["decision_id"],
            )
            assert row is not None
            decision = copy.deepcopy(row.decision)
            decision["outcome"] = "denied"
            row.decision = decision
        token = created.decision["decision_id"].rsplit(":", 1)[1]

        with self.assertRaisesRegex(
            RepositoryCustodyReadinessUnavailable,
            "ledger integrity",
        ):
            self.service.read(token, actor="workspace-governance-control-fabric")

    def test_contract_bundle_rejects_changed_upstream_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            contract_root = Path(temp_dir) / "repository-custody"
            shutil.copytree(
                Path(__file__).resolve().parents[1] / "contracts/repository-custody",
                contract_root,
            )
            authority = contract_root / "repository-custody.yaml"
            authority.write_bytes(authority.read_bytes() + b"\n")
            with self.assertRaisesRegex(
                RepositoryCustodyContractError,
                "does not match its manifest digest",
            ):
                RepositoryCustodyContractBundle.load(contract_root)

    def test_normal_runtime_remains_blocked_by_upstream_activation(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "WGCF_RUNTIME_PROFILE": "dev-integration",
                "WGCF_REPOSITORY_CUSTODY_READINESS_ENABLED": "true",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(
                RepositoryCustodyReadinessUnavailable,
                "authority has not activated",
            ):
                build_repository_custody_readiness_runtime()


if __name__ == "__main__":
    import unittest

    unittest.main()
