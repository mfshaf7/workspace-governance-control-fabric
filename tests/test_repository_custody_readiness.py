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
                "provider_repository_id": "R_kgDOExample",
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

    def test_repository_custody_readiness_positive_conformance(self) -> None:
        created = self.service.issue(self.request(), actor="operator-orchestration-service")
        replay = self.service.issue(self.request(), actor="operator-orchestration-service")
        token = created.decision["decision_id"].rsplit(":", 1)[1]
        read = self.service.read(token, actor="workspace-governance-control-fabric")

        self.assertEqual("allowed", created.decision["outcome"])
        self.assertEqual("read-provider", created.decision["next_action"])
        self.assertEqual(
            {"provider": "github", "provider_repository_id": "R_kgDOExample"},
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

    def test_only_first_active_capability_is_allowed(self) -> None:
        request = json.loads(self.request(action="provision-new"))
        request["target"]["provider_repository_id"] = None
        request.pop("request_digest")
        request["request_digest"] = canonical_digest(request)

        result = self.service.issue(
            canonical_json_bytes(request),
            actor="operator-orchestration-service",
        )

        self.assertEqual("denied", result.decision["outcome"])
        self.assertEqual("custody-action-not-active", result.decision["findings"][0]["code"])

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
