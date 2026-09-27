from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from control_fabric_core.canonical_json import canonical_digest, canonical_json_bytes
from control_fabric_core.db.models import (
    EscalationRecord,
    LedgerEvent,
    LifecycleTransitionReadinessRecord,
    metadata,
)
from control_fabric_core.lifecycle_transition_contracts import (
    LifecycleTransitionContractBundle,
    LifecycleTransitionContractError,
)
from control_fabric_core.lifecycle_transition_readiness import (
    LifecycleTransitionConflict,
    LifecycleTransitionNotFound,
    LifecycleTransitionReadinessService,
    LifecycleTransitionRequestError,
    LifecycleTransitionUnavailable,
    build_lifecycle_transition_readiness_runtime,
)


ROOT = Path(__file__).resolve().parents[1]
IMPLEMENTATION_REF = "1" * 40
IDENTITY = "spiffe://workspace.local/ns/wgcf/sa/control-fabric-api"


class LifecycleTransitionReadinessTests(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.contracts = LifecycleTransitionContractBundle.load()
        self.service = LifecycleTransitionReadinessService(
            session_factory=self.sessions,
            service_identity_ref=IDENTITY,
            implementation_ref=IMPLEMENTATION_REF,
            contract_bundle=self.contracts,
            clock=lambda: datetime(2026, 9, 27, 12, 5, tzinfo=timezone.utc),
        )
        self.projection = json.loads(
            (self.contracts.root / "prototype-to-delivery.current.valid.json").read_text(
                encoding="utf-8",
            )
        )

    def tearDown(self) -> None:
        self.engine.dispose()

    def request(self, projection: dict[str, object] | None = None) -> bytes:
        payload = {
            "schema_version": 1,
            "artifact_type": "lifecycle-transition-readiness-evaluation",
            "evaluation_id": "lifecycle-transition-evaluation:prototype-governance-console:1",
            "profile_id": "dev-integration",
            "projection": copy.deepcopy(projection or self.projection),
        }
        payload["evaluation_digest"] = canonical_digest(payload)
        return canonical_json_bytes(payload)

    def test_all_admitted_routes_issue_current_readiness(self) -> None:
        cases = {
            "prototype-to-delivery": ("prototype", "delivery"),
            "proposal-to-delivery": ("proposal", "delivery"),
            "proposal-to-prototype": ("proposal", "prototype"),
        }
        for index, (route_id, domains) in enumerate(cases.items(), start=1):
            with self.subTest(route_id=route_id):
                projection = copy.deepcopy(self.projection)
                projection["projection"]["route_id"] = route_id
                projection["projection"]["source"]["domain"] = domains[0]
                projection["projection"]["target"]["domain"] = domains[1]
                projection["projection"]["transition_id"] = f"transition:{route_id}:{index}"
                projection["projection"]["correlation_id"] = f"correlation:{route_id}:{index}"
                request = json.loads(self.request(projection))
                request["evaluation_id"] = f"evaluation:{route_id}:{index}"
                request["evaluation_digest"] = canonical_digest(
                    {key: value for key, value in request.items() if key != "evaluation_digest"}
                )

                result = self.service.issue(
                    canonical_json_bytes(request),
                    actor="operator-orchestration-service",
                )

                self.assertEqual("ready", result["readiness"]["outcome"])
                self.assertEqual(route_id, result["readiness"]["transition"]["route_id"])
                self.assertEqual("created", result["ledger"]["resolution"])

    def test_blocked_gate_emits_bounded_escalation_and_durable_ledger(self) -> None:
        projection = copy.deepcopy(self.projection)
        gate = {
            "evidence_ref": "wgcf://evidence/gate/repository-ready",
            "gate_id": "repository-ready",
            "owner_ref": "repository",
            "required_fix": "Complete repository admission.",
            "state": "blocked",
        }
        projection["projection"]["state"] = "blocked"
        projection["projection"]["validation"] = {
            "gates": [gate],
            "receipt_ref": None,
            "run_ref": "wgcf://runs/lifecycle-transition/1",
            "state": "blocked",
        }
        projection["projection"]["blocked_gate"] = copy.deepcopy(gate)
        projection["projection"]["next_action"] = {
            "action": "resolve-gate",
            "owner_ref": "repository",
            "review_at": None,
        }

        result = self.service.issue(
            self.request(projection),
            actor="operator-orchestration-service",
        )

        readiness = result["readiness"]
        self.assertEqual("blocked", readiness["outcome"])
        self.assertEqual("repository", readiness["escalation"]["owner_ref"])
        self.assertEqual("repository-ready", readiness["escalation"]["reason_code"])
        self.assertIn("wgcf://evidence/gate/repository-ready", readiness["evidence_refs"])
        with self.sessions() as session:
            self.assertEqual(1, len(session.scalars(select(EscalationRecord)).all()))
            self.assertEqual(1, len(session.scalars(select(LifecycleTransitionReadinessRecord)).all()))
            self.assertEqual(1, len(session.scalars(select(LedgerEvent)).all()))

    def test_returned_transition_routes_correction_without_mutation(self) -> None:
        projection = copy.deepcopy(self.projection)
        projection["projection"]["state"] = "returned"
        projection["projection"]["correction"] = {
            "owner_ref": "prototype",
            "reason_code": "baseline-evidence-incomplete",
            "required_fix": "Publish the missing baseline evidence reference.",
        }
        projection["projection"]["next_action"] = {
            "action": "correct-source",
            "owner_ref": "prototype",
            "review_at": None,
        }

        result = self.service.issue(self.request(projection), actor="operator-orchestration-service")

        self.assertEqual("requires-action", result["readiness"]["outcome"])
        self.assertEqual("prototype", result["readiness"]["escalation"]["owner_ref"])
        self.assertEqual("correct-source", result["readiness"]["next_action"]["action"])

    def test_applied_transition_requires_complete_target_evidence(self) -> None:
        projection = copy.deepcopy(self.projection)
        transition = projection["projection"]
        transition["state"] = "applied"
        transition["admission"] = {
            "reason_code": None,
            "receipt_ref": "oos://receipts/admission/1",
            "recorded_at": "2026-09-27T12:03:00Z",
            "state": "admitted",
            "target_record_ref": "delivery://work/900",
        }
        transition["application"] = {
            "adapter_ref": "delivery-ingress-adapter",
            "evidence_kind": "target-application-receipt",
            "failure_code": None,
            "failure_detail": None,
            "receipt_ref": "oos://receipts/application/1",
            "recorded_at": "2026-09-27T12:04:00Z",
            "resulting_refs": ["delivery://work/900"],
            "retryable": None,
            "run_ref": "oos://runs/application/1",
            "state": "applied",
            "target_record_ref": "delivery://work/900",
        }
        transition["next_action"] = None

        result = self.service.issue(
            self.request(projection),
            actor="operator-orchestration-service",
        )

        self.assertEqual("terminal", result["readiness"]["outcome"])

        incomplete = copy.deepcopy(projection)
        incomplete["projection"]["application"]["receipt_ref"] = None
        raw = json.loads(self.request(incomplete))
        raw["evaluation_id"] = "evaluation:applied:incomplete"
        raw["evaluation_digest"] = canonical_digest(
            {key: value for key, value in raw.items() if key != "evaluation_digest"}
        )
        blocked = self.service.issue(
            canonical_json_bytes(raw),
            actor="operator-orchestration-service",
        )
        self.assertEqual("blocked", blocked["readiness"]["outcome"])
        self.assertEqual(
            "lifecycle-transition-state-inconsistent",
            blocked["readiness"]["findings"][0]["code"],
        )

    def test_cancelled_and_superseded_require_terminal_reason_bindings(self) -> None:
        cases = (
            ("cancelled", "cancelled_reason_code", "operator-cancelled"),
            ("superseded", "superseded_by_transition_id", "transition:replacement:1"),
        )
        for index, (state, field, value) in enumerate(cases, start=1):
            with self.subTest(state=state):
                projection = copy.deepcopy(self.projection)
                projection["projection"]["state"] = state
                projection["projection"][field] = value
                projection["projection"]["next_action"] = None
                request = json.loads(self.request(projection))
                request["evaluation_id"] = f"evaluation:{state}:{index}"
                request["evaluation_digest"] = canonical_digest(
                    {key: value for key, value in request.items() if key != "evaluation_digest"}
                )

                result = self.service.issue(
                    canonical_json_bytes(request),
                    actor="operator-orchestration-service",
                )

                self.assertEqual("terminal", result["readiness"]["outcome"])

    def test_stale_source_and_unsafe_evidence_fail_closed(self) -> None:
        projection = copy.deepcopy(self.projection)
        projection["freshness"]["state"] = "stale"
        projection["projection"]["history"]["entries"][0]["evidence_refs"].append(
            "https://workspace.local/evidence?token=secret",
        )

        result = self.service.issue(self.request(projection), actor="operator-orchestration-service")

        codes = {finding["code"] for finding in result["readiness"]["findings"]}
        self.assertEqual("blocked", result["readiness"]["outcome"])
        self.assertIn("lifecycle-transition-source-not-current", codes)
        self.assertIn("lifecycle-transition-evidence-reference-unsafe", codes)
        self.assertNotIn("token=secret", json.dumps(result["readiness"]["evidence_refs"]))

    def test_issue_is_idempotent_and_readback_is_caller_scoped(self) -> None:
        raw = self.request()
        created = self.service.issue(raw, actor="operator-orchestration-service")
        reused = self.service.issue(raw, actor="operator-orchestration-service")
        token = created["readiness"]["readiness_digest"].removeprefix("sha256:")
        read = self.service.read(token, actor="operator-orchestration-service")

        self.assertEqual("created", created["ledger"]["resolution"])
        self.assertEqual("reused", reused["ledger"]["resolution"])
        self.assertEqual("read", read["ledger"]["resolution"])
        self.assertEqual(created["readiness"], reused["readiness"])
        self.assertEqual(created["readiness"], read["readiness"])
        with self.assertRaises(LifecycleTransitionNotFound):
            self.service.read(token, actor="workspace-governance-control-fabric")

    def test_conflicting_replay_and_malformed_digest_are_rejected(self) -> None:
        self.service.issue(self.request(), actor="operator-orchestration-service")
        changed = json.loads(self.request())
        changed["projection"]["projection"]["reason"]["detail"] = "Different request content."
        changed["evaluation_digest"] = canonical_digest(
            {key: value for key, value in changed.items() if key != "evaluation_digest"}
        )
        with self.assertRaises(LifecycleTransitionConflict):
            self.service.issue(
                canonical_json_bytes(changed),
                actor="operator-orchestration-service",
            )

        malformed = json.loads(self.request())
        malformed["evaluation_digest"] = "sha256:" + "0" * 64
        with self.assertRaises(LifecycleTransitionRequestError):
            self.service.issue(
                canonical_json_bytes(malformed),
                actor="operator-orchestration-service",
            )

    def test_contract_tamper_and_unactivated_runtime_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            contract_root = Path(temp_dir) / "lifecycle-transition"
            contract_root.mkdir()
            for source in self.contracts.root.iterdir():
                if source.is_file():
                    (contract_root / source.name).write_bytes(source.read_bytes())
            (contract_root / "project-lifecycle.yaml").write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(LifecycleTransitionContractError):
                LifecycleTransitionContractBundle.load(contract_root)

        with patch.dict(
            "os.environ",
            {
                "WGCF_RUNTIME_PROFILE": "dev-integration",
                "WGCF_LIFECYCLE_TRANSITION_READINESS_ENABLED": "false",
            },
            clear=False,
        ):
            with self.assertRaises(LifecycleTransitionUnavailable):
                build_lifecycle_transition_readiness_runtime()
