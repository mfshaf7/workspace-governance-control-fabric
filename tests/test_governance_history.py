from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

from jsonschema import Draft202012Validator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core.db.models import (  # noqa: E402
    Base,
    ControlReceipt,
    EscalationRecord,
    LedgerEvent,
    LifecycleTransitionReadinessRecord,
    ReadinessDecision,
    SourceSnapshot,
)
from control_fabric_core.governance_history import (  # noqa: E402
    GovernanceHistoryAuthorizer,
    GovernanceHistoryNotFound,
    GovernanceHistoryRequestError,
    GovernanceHistoryService,
    GovernanceHistoryUnauthorized,
)


class GovernanceHistoryTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        database = Path(self.temp.name) / "history.sqlite"
        self.engine = create_engine(f"sqlite:///{database}")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.service = GovernanceHistoryService(self.sessions)
        self.now = datetime.now(UTC).replace(microsecond=0)
        self._seed()

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp.cleanup()

    def _seed(self) -> None:
        with self.sessions.begin() as session:
            session.add(
                SourceSnapshot(
                    snapshot_id="source-snapshot:test",
                    actor="agent-gary",
                    source_roots=[],
                    authority_refs=[],
                    digests={},
                    excluded_refs=[],
                    created_at=self.now - timedelta(minutes=5),
                )
            )
            session.add(
                ControlReceipt(
                    receipt_id="control-receipt:test",
                    source_snapshot_id="source-snapshot:test",
                    target="component:console",
                    profile_id="dev-integration",
                    outcome="success",
                    findings=[],
                    suppressed_output_summary={"line_count": 12},
                    artifact_refs=[
                        {"ref": "artifact://wgcf/validation/one"},
                        {"ref": "file:///tmp/raw.log"},
                    ],
                    next_required_action={"authority": "operator", "code": "inspect"},
                    created_at=self.now - timedelta(minutes=4),
                )
            )
            session.add(
                ReadinessDecision(
                    decision_id="readiness-decision:test",
                    target="prototype:alpha",
                    profile_id="dev-integration",
                    outcome="blocked",
                    reasons=[{"code": "evidence-missing", "detail": "not projected"}],
                    authority_refs=[{"ref": "workspace-governance://project-lifecycle"}],
                    receipt_refs=[{"ref": "receipt://wgcf/readiness/one"}],
                    escalation_path_when_blocked={
                        "authority": "workspace-prototype-studio",
                        "code": "repair-evidence",
                    },
                    created_at=self.now - timedelta(minutes=3),
                )
            )
            session.add(
                LifecycleTransitionReadinessRecord(
                    evaluation_id="lifecycle-transition:test",
                    evaluation_digest="sha256:" + "1" * 64,
                    readiness_digest="sha256:" + "2" * 64,
                    actor="operator-orchestration-service",
                    transition_id="prototype-to-delivery",
                    source_revision="a" * 40,
                    contract_digest="sha256:" + "3" * 64,
                    implementation_ref="b" * 40,
                    outcome="ready",
                    expires_at=self.now + timedelta(hours=1),
                    readiness={
                        "evidence_refs": [
                            {"ref": "oos://lifecycle-transitions/transition-1"},
                            {"ref": "/tmp/private-output.json"},
                        ],
                        "next_action": {
                            "authority": "operator-orchestration-service",
                            "code": "apply-transition",
                        },
                        "secret_token": "must-not-appear",
                    },
                    created_at=self.now - timedelta(minutes=2),
                )
            )
            session.add(
                EscalationRecord(
                    escalation_id="escalation:test",
                    trigger_id="prototype:alpha",
                    target_system="operator-orchestration-service",
                    owner_repo="workspace-prototype-studio",
                    required_record="prototype-evidence-repair",
                    evidence_refs=[{"ref": "openproject://work_packages/1198"}],
                    operator_action_required="Repair the missing Prototype evidence reference.",
                    created_at=self.now - timedelta(minutes=1),
                )
            )
            session.add(
                LedgerEvent(
                    event_id="ledger-event:test",
                    event_time=self.now,
                    actor="agent-gary",
                    action="governance.history.tested",
                    target="prototype:alpha",
                    source_snapshot_id=None,
                    outcome="success",
                    receipt_refs=[{"ref": "receipt://wgcf/history/test"}],
                )
            )

    def test_list_is_chronological_bounded_and_safe(self) -> None:
        result = self.service.list(limit=3)

        self.assertEqual(result["projection_state"], "complete")
        self.assertEqual(result["page"]["returned"], 3)
        self.assertTrue(result["page"]["has_more"])
        self.assertIsNotNone(result["page"]["next_cursor"])
        self.assertEqual(result["records"][0]["source"], "ledger-events")
        self.assertNotIn("must-not-appear", json.dumps(result))
        self.assertNotIn("file:///tmp/raw.log", json.dumps(result))
        self.assertNotIn("/tmp/private-output.json", json.dumps(result))

    def test_keyset_pages_do_not_repeat_records(self) -> None:
        first = self.service.list(limit=2)
        second = self.service.list(limit=2, cursor=first["page"]["next_cursor"])

        first_ids = {record["history_id"] for record in first["records"]}
        second_ids = {record["history_id"] for record in second["records"]}
        self.assertFalse(first_ids & second_ids)

    def test_filters_and_detail_preserve_owner_routes(self) -> None:
        result = self.service.list(category="escalation", subject="ALPHA")

        self.assertEqual(result["page"]["returned"], 1)
        record = result["records"][0]
        self.assertEqual(record["next_action"]["authority"], "workspace-prototype-studio")
        self.assertEqual(
            record["evidence_routes"],
            [{"ref": "openproject://work_packages/1198", "route_type": "openproject"}],
        )
        detail = self.service.detail(record["history_id"])
        self.assertEqual(detail["record"], record)

    def test_malformed_cursor_and_identifier_fail_closed(self) -> None:
        with self.assertRaisesRegex(GovernanceHistoryRequestError, "cursor is malformed"):
            self.service.list(cursor="not-a-cursor")
        with self.assertRaises(GovernanceHistoryNotFound):
            self.service.detail("not-a-history-id")

    def test_missing_source_table_is_explicitly_partial(self) -> None:
        database = Path(self.temp.name) / "partial.sqlite"
        engine = create_engine(f"sqlite:///{database}")
        LedgerEvent.__table__.create(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with sessions.begin() as session:
            session.add(
                LedgerEvent(
                    event_id="ledger-event:partial",
                    event_time=self.now,
                    actor="agent-gary",
                    action="history.partial.tested",
                    target="component:console",
                    outcome="success",
                    receipt_refs=[],
                )
            )

        result = GovernanceHistoryService(sessions).list(limit=10)

        self.assertEqual(result["projection_state"], "partial")
        self.assertEqual(result["records"][0]["source"], "ledger-events")
        self.assertTrue(any(source["state"] == "unavailable" for source in result["sources"]))
        engine.dispose()

    def test_dedicated_reader_authentication_is_fail_closed(self) -> None:
        authorizer = GovernanceHistoryAuthorizer(
            caller_id="governance-operations-console",
            caller_secret="s" * 32,
        )
        authorizer.authorize("governance-operations-console", "s" * 32)
        with self.assertRaises(GovernanceHistoryUnauthorized):
            authorizer.authorize("operator-orchestration-service", "s" * 32)
        with self.assertRaises(GovernanceHistoryUnauthorized):
            authorizer.authorize("governance-operations-console", "wrong")

    def test_list_and_detail_match_the_public_projection_schema(self) -> None:
        schema = json.loads(
            (REPO_ROOT / "schemas/governance-history-projection.schema.json").read_text(
                encoding="utf-8",
            )
        )
        validator = Draft202012Validator(schema)
        listed = self.service.list(limit=5)
        detail = self.service.detail(listed["records"][0]["history_id"])

        validator.validate(listed)
        validator.validate(detail)
