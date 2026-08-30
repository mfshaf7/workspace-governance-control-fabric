from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from control_fabric_core.canonical_json import canonical_digest, canonical_json_bytes
from control_fabric_core.db.models import (
    LedgerEvent,
    RepositoryLifecycleDecisionRecord,
    metadata,
)
from control_fabric_core.repository_custody_contracts import RepositoryCustodyContractBundle
from control_fabric_core.repository_lifecycle_readiness import (
    RepositoryLifecycleReadinessConflict,
    RepositoryLifecycleReadinessRequestError,
    RepositoryLifecycleReadinessService,
    RepositoryLifecycleReadinessUnavailable,
    build_repository_lifecycle_readiness_runtime,
)


IMPLEMENTATION_REF = "1" * 40
SERVICE_IDENTITY_REF = "spiffe://workspace.local/ns/wgcf/sa/control-fabric-api"


class RepositoryLifecycleReadinessTests(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.contracts = RepositoryCustodyContractBundle.load()
        self.service = RepositoryLifecycleReadinessService(
            session_factory=self.sessions,
            service_identity_ref=SERVICE_IDENTITY_REF,
            implementation_ref=IMPLEMENTATION_REF,
            contract_bundle=self.contracts,
            clock=lambda: datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc),
        )

    def tearDown(self) -> None:
        self.engine.dispose()

    def request(self, action: str = "transfer-workspace-custody") -> bytes:
        state = {
            "custody_state": "linked",
            "workspace_owner_ref": "repo:source-owner",
            "provider_lifecycle_state": "active",
            "workspace_record_state": "active",
            "custody_version": "custody-version:7",
            "provider_version": "provider-version:11",
        }
        target: dict[str, str | None] = {
            "workspace_owner_ref": "repo:target-owner",
            "provider_lifecycle_state": None,
            "workspace_record_state": None,
        }
        authority = {
            "policy_profile_ref": {
                "uri": self.contracts.authority_uri,
                "digest": self.contracts.authority_digest,
            },
            "approval_ref": self.ref(f"approval/{action}"),
            "source_owner_acceptance_ref": self.ref("acceptance/source"),
            "target_owner_acceptance_ref": self.ref("acceptance/target"),
            "provider_credential_binding_ref": None,
        }
        reversal_ref = None

        if action == "archive-provider":
            target = {
                "workspace_owner_ref": None,
                "provider_lifecycle_state": "archived",
                "workspace_record_state": None,
            }
            authority["source_owner_acceptance_ref"] = None
            authority["target_owner_acceptance_ref"] = None
            authority["provider_credential_binding_ref"] = self.ref(
                "credential-binding/repository-lifecycle",
            )
        elif action == "unarchive-provider":
            state["provider_lifecycle_state"] = "archived"
            target = {
                "workspace_owner_ref": None,
                "provider_lifecycle_state": "active",
                "workspace_record_state": None,
            }
            authority["source_owner_acceptance_ref"] = None
            authority["target_owner_acceptance_ref"] = None
            authority["provider_credential_binding_ref"] = self.ref(
                "credential-binding/repository-lifecycle",
            )
            reversal_ref = self.ref("receipts/archive-provider")
        elif action == "retire-workspace-record":
            target = {
                "workspace_owner_ref": None,
                "provider_lifecycle_state": None,
                "workspace_record_state": "retired",
            }
            authority["source_owner_acceptance_ref"] = None
            authority["target_owner_acceptance_ref"] = None
        elif action == "restore-workspace-record":
            state["workspace_record_state"] = "retired"
            target = {
                "workspace_owner_ref": None,
                "provider_lifecycle_state": None,
                "workspace_record_state": "active",
            }
            authority["source_owner_acceptance_ref"] = None
            authority["target_owner_acceptance_ref"] = None
            reversal_ref = self.ref("receipts/retire-workspace-record")

        payload = {
            "schema_version": 1,
            "artifact_type": "repository_lifecycle_request",
            "request_id": f"repository-lifecycle-request:{action}-001",
            "requested_at": "2026-08-30T01:59:00Z",
            "action": action,
            "operator_ref": self.ref("operators/example"),
            "workflow": {
                "workflow_id": "repository-lifecycle",
                "workflow_version": "1",
                "execution_id": f"repository-lifecycle-{action}-001",
            },
            "repository_identity": {
                "provider": "github",
                "provider_repository_id": "123456789",
            },
            "current_state": state,
            "target": target,
            "impact": {
                "impact_assessment_ref": self.ref(f"impact/{action}"),
                "finding_count": 0,
                "blocking_finding_count": 0,
                "blocker_disposition": None,
            },
            "authority": authority,
            "reversal_of_receipt_ref": reversal_ref,
            "correlation": {
                "correlation_id": f"repository-lifecycle-{action}-001",
                "causation_id": None,
            },
            "idempotency_key": f"repository-lifecycle-{action}-001",
        }
        payload["request_digest"] = canonical_digest(payload)
        return canonical_json_bytes(payload)

    @staticmethod
    def ref(path: str) -> dict[str, str]:
        return {
            "uri": f"https://workspace.local/{path}",
            "digest": "sha256:" + "8" * 64,
        }

    def encode(self, payload: dict[str, object]) -> bytes:
        payload.pop("request_digest", None)
        payload["request_digest"] = canonical_digest(payload)
        return canonical_json_bytes(payload)

    def test_positive_lifecycle_matrix_emits_exact_next_actions_and_gates(self) -> None:
        expected = {
            "transfer-workspace-custody": (
                "apply-workspace-custody",
                [
                    "exact-operator-approval",
                    "source-owner-acceptance",
                    "target-owner-acceptance",
                ],
            ),
            "archive-provider": (
                "archive-provider",
                ["exact-operator-approval", "governed-provider-credential-binding"],
            ),
            "unarchive-provider": (
                "unarchive-provider",
                ["exact-operator-approval", "governed-provider-credential-binding"],
            ),
            "retire-workspace-record": (
                "retire-workspace-record",
                ["exact-operator-approval"],
            ),
            "restore-workspace-record": (
                "restore-workspace-record",
                ["exact-operator-approval"],
            ),
        }

        for action, (next_action, gates) in expected.items():
            with self.subTest(action=action):
                result = self.service.issue(
                    self.request(action),
                    actor="operator-orchestration-service",
                )
                self.assertEqual("allowed", result.decision["outcome"])
                self.assertEqual(next_action, result.decision["next_action"])
                self.assertEqual(gates, result.decision["required_human_gates"])
                self.assertEqual("none", result.decision["impact"]["downstream_mutation"])
                self.assertEqual(
                    "repository-lifecycle-request-ready",
                    result.decision["findings"][0]["code"],
                )

    def test_decision_is_idempotent_readable_and_ledgered(self) -> None:
        created = self.service.issue(
            self.request("archive-provider"),
            actor="operator-orchestration-service",
        )
        replay = self.service.issue(
            self.request("archive-provider"),
            actor="operator-orchestration-service",
        )
        token = created.decision["decision_id"].rsplit(":", 1)[1]
        read = self.service.read(token, actor="workspace-governance-control-fabric")

        self.assertEqual("created", created.resolution)
        self.assertEqual("reused", replay.resolution)
        self.assertEqual("read", read.resolution)
        self.assertEqual(created.decision, replay.decision)
        self.assertEqual(created.decision, read.decision)
        with self.sessions() as session:
            self.assertEqual(
                1,
                len(session.scalars(select(RepositoryLifecycleDecisionRecord)).all()),
            )
            actions = {event.action for event in session.scalars(select(LedgerEvent)).all()}
        self.assertEqual(
            {
                "repository-lifecycle.readiness.persisted",
                "repository-lifecycle.readiness.reused",
                "repository-lifecycle.readiness.read",
            },
            actions,
        )

    def test_stale_policy_and_secret_bearing_refs_are_denied(self) -> None:
        stale = json.loads(self.request())
        stale["authority"]["policy_profile_ref"]["digest"] = "sha256:" + "9" * 64
        stale_result = self.service.issue(
            self.encode(stale),
            actor="operator-orchestration-service",
        )
        self.assertEqual("denied", stale_result.decision["outcome"])
        self.assertEqual("stop", stale_result.decision["next_action"])
        self.assertEqual(
            "repository-lifecycle-policy-binding-stale",
            stale_result.decision["findings"][0]["code"],
        )

        secret = json.loads(self.request("archive-provider"))
        secret["authority"]["provider_credential_binding_ref"]["uri"] += "?token=hidden"
        secret_result = self.service.issue(
            self.encode(secret),
            actor="operator-orchestration-service",
        )
        self.assertEqual("denied", secret_result.decision["outcome"])
        self.assertEqual(
            "provider-credential-binding-reference-secret-bearing",
            secret_result.decision["findings"][0]["code"],
        )

    def test_deferred_impact_requires_correction_without_approval(self) -> None:
        request = json.loads(self.request("retire-workspace-record"))
        request["impact"] = {
            "impact_assessment_ref": self.ref("impact/retire-workspace-record"),
            "finding_count": 2,
            "blocking_finding_count": 1,
            "blocker_disposition": {
                "decision": "defer",
                "justification": "An active consumer must be reviewed first.",
                "evidence_ref": self.ref("impact/retire-workspace-record/defer"),
            },
        }
        result = self.service.issue(
            self.encode(request),
            actor="operator-orchestration-service",
        )

        self.assertEqual("requires-action", result.decision["outcome"])
        self.assertEqual("request-correction", result.decision["next_action"])
        self.assertIsNone(result.decision["approved_target"])
        self.assertEqual(
            "repository-lifecycle-impact-deferred",
            result.decision["findings"][0]["code"],
        )

    def test_accepted_blocker_disposition_can_advance_without_downstream_mutation(self) -> None:
        request = json.loads(self.request("retire-workspace-record"))
        request["impact"] = {
            "impact_assessment_ref": self.ref("impact/retire-workspace-record"),
            "finding_count": 1,
            "blocking_finding_count": 1,
            "blocker_disposition": {
                "decision": "remove",
                "justification": "The consumer link was removed and verified.",
                "evidence_ref": self.ref("impact/retire-workspace-record/remove"),
            },
        }
        result = self.service.issue(
            self.encode(request),
            actor="operator-orchestration-service",
        )

        self.assertEqual("allowed", result.decision["outcome"])
        self.assertEqual("none", result.decision["impact"]["downstream_mutation"])

    def test_invalid_impact_and_provider_authority_fail_closed(self) -> None:
        invalid_impact = json.loads(self.request())
        invalid_impact["impact"] = {
            "impact_assessment_ref": self.ref("impact/transfer"),
            "finding_count": 1,
            "blocking_finding_count": 2,
            "blocker_disposition": {
                "decision": "remove",
                "justification": "Claimed removal.",
                "evidence_ref": self.ref("impact/transfer/remove"),
            },
        }
        result = self.service.issue(
            self.encode(invalid_impact),
            actor="operator-orchestration-service",
        )
        self.assertEqual("denied", result.decision["outcome"])
        self.assertEqual(
            "repository-lifecycle-impact-count-invalid",
            result.decision["findings"][0]["code"],
        )

        missing_version = json.loads(self.request("archive-provider"))
        missing_version["current_state"]["provider_version"] = None
        result = self.service.issue(
            self.encode(missing_version),
            actor="operator-orchestration-service",
        )
        self.assertEqual("denied", result.decision["outcome"])
        self.assertEqual(
            "repository-lifecycle-provider-version-missing",
            result.decision["findings"][0]["code"],
        )

        unsupported_provider = json.loads(self.request("archive-provider"))
        unsupported_provider["request_id"] = (
            "repository-lifecycle-request:archive-provider-gitlab-001"
        )
        unsupported_provider["repository_identity"] = {
            "provider": "gitlab",
            "provider_repository_id": "project-123",
        }
        result = self.service.issue(
            self.encode(unsupported_provider),
            actor="operator-orchestration-service",
        )
        self.assertEqual("denied", result.decision["outcome"])
        self.assertEqual(
            "repository-lifecycle-provider-not-active",
            result.decision["findings"][0]["code"],
        )

    def test_same_request_id_with_changed_content_is_a_conflict(self) -> None:
        self.service.issue(self.request(), actor="operator-orchestration-service")
        changed = json.loads(self.request())
        changed["target"]["workspace_owner_ref"] = "repo:different-target"

        with self.assertRaises(RepositoryLifecycleReadinessConflict):
            self.service.issue(
                self.encode(changed),
                actor="operator-orchestration-service",
            )

    def test_malformed_digest_and_unsupported_action_are_rejected(self) -> None:
        malformed = json.loads(self.request())
        malformed["request_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(
            RepositoryLifecycleReadinessRequestError,
            "does not match canonical request content",
        ):
            self.service.issue(
                canonical_json_bytes(malformed),
                actor="operator-orchestration-service",
            )

        unsupported = json.loads(self.request())
        unsupported["action"] = "delete-provider"
        with self.assertRaisesRegex(
            RepositoryLifecycleReadinessRequestError,
            "delete-provider",
        ):
            self.service.issue(
                self.encode(unsupported),
                actor="operator-orchestration-service",
            )

    def test_tampered_persisted_decision_is_not_readable(self) -> None:
        created = self.service.issue(self.request(), actor="operator-orchestration-service")
        with self.sessions.begin() as session:
            row = session.get(
                RepositoryLifecycleDecisionRecord,
                created.decision["decision_id"],
            )
            assert row is not None
            decision = copy.deepcopy(row.decision)
            decision["outcome"] = "denied"
            row.decision = decision
        token = created.decision["decision_id"].rsplit(":", 1)[1]

        with self.assertRaisesRegex(
            RepositoryLifecycleReadinessUnavailable,
            "ledger integrity",
        ):
            self.service.read(token, actor="workspace-governance-control-fabric")

    def test_normal_runtime_remains_blocked_by_upstream_activation(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "WGCF_RUNTIME_PROFILE": "dev-integration",
                "WGCF_REPOSITORY_LIFECYCLE_READINESS_ENABLED": "true",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(
                RepositoryLifecycleReadinessUnavailable,
                "authority has not activated",
            ):
                build_repository_lifecycle_readiness_runtime()
