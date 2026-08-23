from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import (
    AgentActionContractBundle,
    AgentActionContractError,
    evaluate_agent_action_request,
    run_agent_action_evaluation,
)
from control_fabric_core.canonical_json import canonical_digest


FIXTURE_ROOT = REPO_ROOT / "contracts/agent-action/fixtures"
DECISION_TIME = "2026-08-22T10:00:01Z"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def refresh_digest(request: dict) -> dict:
    request = copy.deepcopy(request)
    request["integrity"].pop("content_digest", None)
    request["integrity"]["content_digest"] = canonical_digest(request)
    return request


def request_for(action_class: str = "mutate") -> dict:
    request = load_fixture("request.valid.json")
    request["action_class"] = action_class
    if action_class == "read":
        request["model_invocation_ref"] = None
        request["context"] = {"packet_ref": None, "receipt_ref": None}
        request["authority"]["approval_ref"] = None
    elif action_class in {"advise", "draft"}:
        request["authority"]["approval_ref"] = None
    elif action_class == "mutate":
        request["model_invocation_ref"] = None
    return refresh_digest(request)


def current_bindings() -> dict:
    return load_fixture("current.valid.json")


class AgentActionPolicyTests(TestCase):
    def test_pinned_contract_and_valid_request_load(self) -> None:
        bundle = AgentActionContractBundle.load()
        request = request_for()

        bundle.validate("agent_action_request", request)

        self.assertEqual(bundle.authority["owner_repo"], "workspace-governance")
        self.assertEqual(
            bundle.source_commit,
            "d6e5a5bf0cac6ddfbf127f5826159556971c3718",
        )

    def test_pinned_contract_rejects_changed_authority_bytes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            bundle_root = Path(temp_dir) / "agent-action"
            shutil.copytree(REPO_ROOT / "contracts/agent-action", bundle_root)
            authority_path = bundle_root / "agent-action-authority.yaml"
            authority_path.write_text(
                authority_path.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                AgentActionContractError,
                "does not match its manifest digest",
            ):
                AgentActionContractBundle.load(bundle_root)

    def test_each_action_class_can_issue_an_allow_decision(self) -> None:
        for action_class in ("read", "advise", "draft", "mutate"):
            with self.subTest(action_class=action_class):
                decision = evaluate_agent_action_request(
                    request_for(action_class),
                    current=current_bindings(),
                    now=DECISION_TIME,
                ).to_record()

                self.assertEqual(decision["action_class"], action_class)
                self.assertEqual(decision["outcome"], "allow")
                self.assertIn("record-terminal-action-receipt", decision["obligations"])
                self.assertIn("require-current-source-version", decision["obligations"])
                if action_class == "mutate":
                    self.assertIn(
                        "require-exact-operator-approval",
                        decision["obligations"],
                    )
                    self.assertIn(
                        "require-owner-receipt-after-invocation",
                        decision["obligations"],
                    )
                else:
                    self.assertNotIn(
                        "require-exact-operator-approval",
                        decision["obligations"],
                    )

    def test_unproven_current_bindings_require_review(self) -> None:
        decision = evaluate_agent_action_request(
            request_for(),
            current={},
            now=DECISION_TIME,
        ).to_record()

        self.assertEqual(decision["outcome"], "review-required")
        self.assertIn("caller-identity-unverified", decision["reason_codes"])
        self.assertIn("source-version-unverified", decision["reason_codes"])

    def test_identity_and_caller_mismatches_deny(self) -> None:
        cases = {
            "operator-session-stale": (
                "operator_session_ref",
                {"uri": "wgcf://sessions/stale", "digest": "sha256:" + "1" * 64},
            ),
            "caller-identity-mismatch": ("caller_workload_id", "untrusted-caller"),
            "agent-instance-stale": ("agent_instance_id", "agent-instance:stale"),
        }
        for reason, (field, value) in cases.items():
            with self.subTest(reason=reason):
                current = current_bindings()
                current[field] = value
                decision = evaluate_agent_action_request(
                    request_for(),
                    current=current,
                    now=DECISION_TIME,
                ).to_record()
                self.assertEqual(decision["outcome"], "deny")
                self.assertIn(reason, decision["reason_codes"])

    def test_workflow_target_version_and_context_mismatches_deny(self) -> None:
        cases = {
            "workflow-command-not-admitted": ("admitted_commands", ["read-work-item"]),
            "target-resource-mismatch": ("target_resource_id", "work-item-999"),
            "source-version-mismatch": ("source_version", "lock-version:5"),
            "context-receipt-mismatch": (
                "context_receipt_ref",
                {"uri": "cgg://receipts/stale", "digest": "sha256:" + "7" * 64},
            ),
        }
        for reason, (field, value) in cases.items():
            with self.subTest(reason=reason):
                current = current_bindings()
                current[field] = value
                decision = evaluate_agent_action_request(
                    request_for(),
                    current=current,
                    now=DECISION_TIME,
                ).to_record()
                self.assertEqual(decision["outcome"], "deny")
                self.assertIn(reason, decision["reason_codes"])

    def test_expired_request_and_approval_deny(self) -> None:
        request_decision = evaluate_agent_action_request(
            request_for(),
            current=current_bindings(),
            now="2026-08-22T10:15:00Z",
        ).to_record()
        self.assertEqual(request_decision["outcome"], "deny")
        self.assertIn("request-expired", request_decision["reason_codes"])

        current = current_bindings()
        current["approval_expires_at"] = DECISION_TIME
        approval_decision = evaluate_agent_action_request(
            request_for(),
            current=current,
            now=DECISION_TIME,
        ).to_record()
        self.assertEqual(approval_decision["outcome"], "deny")
        self.assertIn("approval-expired", approval_decision["reason_codes"])

    def test_replayed_or_conflicting_idempotency_key_denies(self) -> None:
        request = request_for()
        cases = {
            "idempotency-key-consumed": request["intent"]["digest"],
            "idempotency-intent-conflict": "sha256:" + "f" * 64,
        }
        for reason, intent_digest in cases.items():
            with self.subTest(reason=reason):
                current = current_bindings()
                current["consumed_idempotency"] = [
                    {
                        "idempotency_key": request["idempotency_key"],
                        "intent_digest": intent_digest,
                    },
                ]
                decision = evaluate_agent_action_request(
                    request,
                    current=current,
                    now=DECISION_TIME,
                ).to_record()
                self.assertEqual(decision["outcome"], "deny")
                self.assertIn(reason, decision["reason_codes"])

    def test_tampered_request_digest_is_rejected_before_decision(self) -> None:
        request = request_for()
        request["target"]["resource_id"] = "work-item-tampered"

        with self.assertRaisesRegex(
            AgentActionContractError,
            "integrity.content_digest does not match",
        ):
            evaluate_agent_action_request(
                request,
                current=current_bindings(),
                now=DECISION_TIME,
            )

    def test_decision_is_bounded_digest_bound_and_schema_valid(self) -> None:
        bundle = AgentActionContractBundle.load()
        decision = evaluate_agent_action_request(
            request_for(),
            current=current_bindings(),
            contract_bundle=bundle,
            now=DECISION_TIME,
        ).to_record()

        bundle.validate("agent_action_policy_decision", decision)
        projection = copy.deepcopy(decision)
        projection["integrity"].pop("content_digest")
        self.assertEqual(decision["integrity"]["content_digest"], canonical_digest(projection))
        serialized = json.dumps(decision)
        self.assertNotIn("raw_context", serialized)
        self.assertNotIn("credentials", serialized)
        self.assertNotIn("backend_output", serialized)

    def test_run_appends_compact_decision_event(self) -> None:
        with TemporaryDirectory() as temp_dir:
            ledger_path = Path(temp_dir) / "ledger.jsonl"
            result = run_agent_action_evaluation(
                request_for(),
                actor="operator-orchestration-service",
                current=current_bindings(),
                ledger_path=ledger_path,
                now=DECISION_TIME,
            )

            line = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(line, result.ledger_event.to_record())
            self.assertEqual(line["outcome"], "allow")
            self.assertEqual(
                line["receipt_refs"][0]["receipt_id"],
                result.decision.decision_id,
            )
