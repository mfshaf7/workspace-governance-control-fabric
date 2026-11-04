from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock

from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import sessionmaker

from control_fabric_core.db.models import (
    InstanceOperationalOperation as Operation,
    InstanceOperationalRecord as Record,
    InstanceOperationalRevision as Revision,
    metadata,
)
from control_fabric_core.instance_records import (
    InstanceRecordConflict, InstanceRecordInvalid, InstanceRecordStore,
    InstanceRecordUnavailable, record_digest,
)


class InstanceRecordStoreTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.url = f"sqlite:///{Path(self.temp.name) / 'instance.sqlite'}"
        self.engine = create_engine(self.url)
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        # This simulates an already verified server-side policy decision. It
        # is deliberately not exposed as a client API or production validator.
        self.validator = Mock(return_value={
            "policy_ref": "governance-policy://exact-reviewed-policy",
            "decision_digest": record_digest({"accepted": "operator"}),
            "readiness_digest": record_digest({"outcome": "allowed"}),
            "operator_ref": "operator:test",
        })
        self.store = self.new_store()

    def new_store(self):
        return InstanceRecordStore(session_factory=self.sessions,
                                   validate_transition=self.validator,
                                   clock=lambda: datetime(2026, 11, 4, tzinfo=timezone.utc))

    def command(self, *, name="client-project", instance="installation-a", provider_id="123", expected=None, key="request-one", phase="intake"):
        return {"schema_version": 1, "instance_id": instance, "record_id": f"repo:{name}",
                "idempotency_key": key, "expected": expected or {"version": None, "digest": None},
                "action": "classify" if phase == "intake" else "promote",
                "record": {"phase": phase, "status": "proposed" if phase == "intake" else "active",
                           "notes": "Installation-private project description",
                           "provider_repository": {"provider": "github", "provider_repository_id": provider_id,
                                                   "full_name": f"example-org/{name}"}},
                "decision": {"ref": "operator:test/accepted"}}

    def apply(self, command):
        return self.store.commit(command, actor="oos:test")

    def expected(self, result):
        receipt = result["receipt"]
        return {"version": receipt["version"], "digest": receipt["record_digest"]}

    def count(self, model):
        with self.sessions() as session:
            return len(list(session.scalars(select(model))))

    def test_commit_persists_record_history_receipt_and_reloads_after_process_restart(self):
        result = self.apply(self.command())
        self.assertEqual(result["receipt"]["version"], 1)
        self.assertEqual(result["receipt"]["authority"], "internal-instance-record")
        self.assertNotIn("merge_commit", result["receipt"])
        self.assertEqual(self.count(Record), 1)
        self.assertEqual(self.count(Revision), 1)
        self.assertEqual(self.count(Operation), 1)
        restarted = self.new_store()
        self.assertEqual(restarted.read(instance_id="installation-a", record_id="repo:client-project"), result)
        self.assertEqual(restarted.commit(self.command(), actor="oos:test"), result)

    def test_exact_provider_lookup_uses_the_configured_installation_and_preserves_canonical_alias(self):
        first = self.apply(self.command(name="canonical-alias", provider_id="345", key="provider-one"))
        self.apply(self.command(name="foreign-alias", provider_id="345", instance="installation-b", key="provider-foreign"))
        self.assertEqual(self.store.read_by_provider(instance_id="installation-a", provider="github", provider_repository_id="345"), first)
        self.assertIsNone(self.store.read_by_provider(instance_id="installation-a", provider="github", provider_repository_id="999"))
        with self.assertRaises(InstanceRecordInvalid): self.store.read_by_provider(instance_id="installation-a", provider="github", provider_repository_id="0")

    def test_lost_response_replay_returns_original_after_later_promotion(self):
        first = self.apply(self.command())
        promoted = self.apply(self.command(key="request-two", expected=self.expected(first), phase="inventory"))
        self.assertEqual(promoted["receipt"]["version"], 2)
        self.assertEqual(self.apply(self.command()), first)
        self.assertEqual(self.count(Record), 1)
        self.assertEqual(self.count(Revision), 2)
        self.assertEqual(self.validator.call_count, 2)

    def test_same_idempotency_key_with_changed_content_or_caller_fails(self):
        command = self.command()
        self.apply(command)
        changed = copy.deepcopy(command)
        changed["record"]["notes"] = "Changed after acceptance"
        with self.assertRaises(InstanceRecordConflict):
            self.apply(changed)
        with self.assertRaises(InstanceRecordConflict):
            self.store.commit(command, actor="different-consumer")
        self.assertEqual(self.count(Revision), 1)

    def test_stale_or_partial_expected_state_never_overwrites(self):
        first = self.apply(self.command())
        for expected in [{"version": None, "digest": None}, {"version": 2, "digest": first["receipt"]["record_digest"]}]:
            with self.assertRaises(InstanceRecordConflict):
                self.apply(self.command(key="second", expected=expected))
        with self.assertRaises(InstanceRecordInvalid):
            self.apply(self.command(key="third", expected={"version": 1, "digest": None}))
        self.assertEqual(self.count(Revision), 1)

    def test_server_validator_denies_before_any_write_and_is_mandatory(self):
        self.validator.side_effect = InstanceRecordConflict("readiness denied")
        with self.assertRaises(InstanceRecordConflict):
            self.apply(self.command())
        for model in (Record, Revision, Operation):
            self.assertEqual(self.count(model), 0)
        with self.assertRaises(InstanceRecordUnavailable):
            InstanceRecordStore(session_factory=self.sessions, validate_transition=None)

    def test_caller_allowed_flag_cannot_replace_server_validation(self):
        command = self.command()
        command["allowed"] = True
        with self.assertRaises(InstanceRecordInvalid):
            self.apply(command)
        self.validator.return_value = {"allowed": True}
        with self.assertRaises(InstanceRecordUnavailable):
            self.apply(self.command())
        self.assertEqual(self.count(Record), 0)

    def test_provider_identity_unique_within_installation_and_isolated_between_installations(self):
        self.apply(self.command())
        with self.assertRaises(InstanceRecordConflict):
            self.apply(self.command(name="other-name", key="other-request"))
        other = self.apply(self.command(instance="installation-b"))
        self.assertEqual(other["receipt"]["version"], 1)
        self.assertEqual(len(self.store.list(instance_id="installation-a")["records"]), 1)
        self.assertEqual(len(self.store.list(instance_id="installation-b")["records"]), 1)
        self.assertIsNone(self.store.read(instance_id="installation-c", record_id="repo:client-project"))

    def test_accepted_ai_suggestion_cannot_authorize_two_different_records(self):
        self.validator.return_value["ai_decision_id"] = "synthetic-ai-decision"
        first = self.apply(self.command())
        self.assertEqual(first["receipt"]["ai_decision_id"], "synthetic-ai-decision")
        with self.assertRaises(InstanceRecordConflict):
            self.apply(self.command(name="another-project", provider_id="456", key="another-operation"))
        self.assertEqual(self.count(Record), 1)
        self.assertEqual(self.apply(self.command()), first)

    def test_provider_substitution_or_removal_denied_but_reviewed_coordinate_change_allowed(self):
        first = self.apply(self.command())
        second = self.command(key="rename", expected=self.expected(first))
        second["record"]["provider_repository"]["full_name"] = "example-org/new-name"
        renamed = self.apply(second)
        self.assertEqual(renamed["receipt"]["version"], 2)
        for provider_id in ("999", None):
            command = self.command(key=f"changed-{provider_id}", expected=self.expected(renamed), provider_id=provider_id)
            if provider_id is None:
                command["record"].pop("provider_repository")
            with self.assertRaises(InstanceRecordConflict):
                self.apply(command)

    def test_transaction_failure_rolls_back_all_three_writes(self):
        def interrupt_before_receipt(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO instance_operational_revisions"):
                raise RuntimeError("simulated interruption before commit")
        event.listen(self.engine, "before_cursor_execute", interrupt_before_receipt)
        try:
            with self.assertRaises(RuntimeError):
                self.apply(self.command())
        finally:
            event.remove(self.engine, "before_cursor_execute", interrupt_before_receipt)
        for model in (Record, Revision, Operation):
            self.assertEqual(self.count(model), 0)
        self.assertEqual(self.apply(self.command())["receipt"]["version"], 1)

    def run_interrupted_process(self, point):
        script = """
import json, os, sys
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from control_fabric_core.instance_records import InstanceRecordStore
command, evidence, url, point = json.loads(sys.stdin.read())
engine = create_engine(url)
if point == 'before-commit':
    def interrupt(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO instance_operational_revisions'):
            os._exit(7)
    event.listen(engine, 'before_cursor_execute', interrupt)
store = InstanceRecordStore(session_factory=sessionmaker(engine), validate_transition=lambda *args: evidence)
store.commit(command, actor='oos:test')
os._exit(7)
"""
        process = subprocess.run([sys.executable, "-c", script],
                                 input=json.dumps([self.command(), self.validator.return_value, self.url, point]),
                                 text=True, capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 7, process.stderr)

    def test_process_exit_after_commit_recovers_original_result_without_a_second_decision(self):
        self.run_interrupted_process("after-commit")
        result = self.apply(self.command())
        self.assertEqual(result["receipt"]["version"], 1)
        self.assertEqual(self.count(Operation), 1)
        self.assertEqual(self.count(Revision), 1)
        self.validator.assert_not_called()

    def test_process_exit_with_open_transaction_recovers_without_partial_records(self):
        self.run_interrupted_process("before-commit")
        for model in (Record, Revision, Operation):
            self.assertEqual(self.count(model), 0)
        self.assertEqual(self.apply(self.command())["receipt"]["version"], 1)

    def test_corrupt_current_record_or_revision_or_replay_fails_closed(self):
        result = self.apply(self.command())
        with self.sessions.begin() as session:
            session.execute(update(Record).values(value={"corrupt": True}))
        with self.assertRaises(InstanceRecordUnavailable):
            self.store.read(instance_id="installation-a", record_id="repo:client-project")
        with self.sessions.begin() as session:
            session.execute(update(Record).values(value=result["record"]))
            session.execute(update(Revision).values(receipt={"corrupt": True}))
        with self.assertRaises(InstanceRecordUnavailable):
            self.apply(self.command())

    def test_history_pages_retain_all_revisions_and_detect_missing_history(self):
        result = self.apply(self.command())
        for index in range(2, 5):
            result = self.apply(self.command(key=f"change-{index}", expected=self.expected(result)))
        page = self.store.history(instance_id="installation-a", record_id="repo:client-project", limit=2)
        self.assertEqual([row["receipt"]["version"] for row in page["records"]], [4, 3])
        older = self.store.history(instance_id="installation-a", record_id="repo:client-project", limit=2,
                                   before_version=page["next_before_version"])
        self.assertEqual([row["receipt"]["version"] for row in older["records"]], [2, 1])
        self.assertIsNone(older["next_before_version"])
        with self.sessions.begin() as session:
            session.delete(session.get(Revision, ("installation-a", "repo:client-project", 2)))
        with self.assertRaises(InstanceRecordUnavailable):
            self.store.history(instance_id="installation-a", record_id="repo:client-project")

    def test_missing_operation_or_altered_original_decision_makes_authority_unavailable(self):
        command = self.command()
        self.apply(command)
        with self.sessions.begin() as session:
            operation = session.get(Operation, ("installation-a", "request-one"))
            altered = copy.deepcopy(operation.command)
            altered["decision"] = {"forged": True}
            operation.command = altered
        with self.assertRaises(InstanceRecordUnavailable):
            self.store.read(instance_id="installation-a", record_id="repo:client-project")

        with self.sessions.begin() as session:
            operation = session.get(Operation, ("installation-a", "request-one"))
            operation.command = command
        self.assertIsNotNone(self.store.read(instance_id="installation-a", record_id="repo:client-project"))
        with self.sessions.begin() as session:
            session.delete(session.get(Operation, ("installation-a", "request-one")))
        with self.assertRaises(InstanceRecordUnavailable):
            self.store.read(instance_id="installation-a", record_id="repo:client-project")

    def test_noncanonical_or_wrong_typed_stored_data_is_an_authority_failure(self):
        self.apply(self.command())
        for corrupt in ({"unexpected": 0.5}, ["unexpected"], None):
            with self.sessions.begin() as session:
                session.execute(update(Record).values(value=corrupt))
            with self.assertRaises(InstanceRecordUnavailable):
                self.store.read(instance_id="installation-a", record_id="repo:client-project")
    def test_record_list_keyset_pagination_has_no_duplicates(self):
        for index in range(3):
            self.apply(self.command(name=f"project-{index}", key=f"request-{index}", provider_id=str(index + 1)))
        first = self.store.list(instance_id="installation-a", limit=2)
        second = self.store.list(instance_id="installation-a", limit=2, after=first["next_after"])
        self.assertEqual(len(first["records"]), 2)
        self.assertEqual(len(second["records"]), 1)
        self.assertIsNone(second["next_after"])

    def test_command_and_read_bounds_fail_before_storage(self):
        for command in [self.command(provider_id="0"), self.command(provider_id="1.5")]:
            with self.assertRaises(InstanceRecordInvalid):
                self.apply(command)
        command = self.command()
        command["record"]["notes"] = "x" * 131_072
        with self.assertRaises(InstanceRecordInvalid):
            self.apply(command)
        for limit in (True, 0, 101):
            with self.assertRaises(InstanceRecordInvalid):
                self.store.list(instance_id="installation-a", limit=limit)
