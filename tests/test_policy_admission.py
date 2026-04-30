from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import build_policy_ledger_event, evaluate_admission_policy


SUCCESS_RECEIPT = {
    "digest": "sha256:" + "a" * 64,
    "outcome": "success",
    "receipt_id": "control-receipt:aaaaaaaaaaaaaaaaaaaaaaaa",
}


def authority_ref(status: str = "current") -> dict:
    return {
        "authority_id": "wgcf-runtime-repo-guidance",
        "digest": "sha256:example",
        "freshness_status": status,
    }


def subject(**overrides) -> dict:
    value = {
        "authority_refs": [authority_ref()],
        "owner_repo": "workspace-governance-control-fabric",
        "receipt_refs": [SUCCESS_RECEIPT],
        "subject_id": "workspace-governance-control-fabric",
        "subject_type": "repo",
    }
    value.update(overrides)
    return value


class PolicyAdmissionTests(TestCase):
    def test_current_authority_and_successful_receipt_allows_admission(self) -> None:
        decision = evaluate_admission_policy(subject(), now="2026-04-30T00:00:00Z")

        self.assertEqual(decision.outcome, "allow")
        self.assertEqual(decision.target, "repo:workspace-governance-control-fabric")
        self.assertEqual(decision.reasons, ())

    def test_missing_authority_requires_review(self) -> None:
        decision = evaluate_admission_policy(
            subject(authority_refs=[]),
            now="2026-04-30T00:00:00Z",
        )

        self.assertEqual(decision.outcome, "review_required")
        self.assertEqual(decision.reasons[0].code, "missing-authority-ref")

    def test_stale_authority_blocks_admission(self) -> None:
        decision = evaluate_admission_policy(
            subject(authority_refs=[authority_ref("stale")]),
            now="2026-04-30T00:00:00Z",
        )

        self.assertEqual(decision.outcome, "blocked")
        self.assertEqual(decision.reasons[0].code, "stale-authority-ref")

    def test_missing_owner_repo_denies_admission(self) -> None:
        decision = evaluate_admission_policy(
            subject(owner_repo=""),
            now="2026-04-30T00:00:00Z",
        )

        self.assertEqual(decision.outcome, "deny")
        self.assertEqual(decision.reasons[0].code, "missing-owner-repo")

    def test_failed_receipt_blocks_without_valid_waiver(self) -> None:
        failed_receipt = {
            "digest": "sha256:" + "b" * 64,
            "outcome": "failure",
            "receipt_id": "control-receipt:bbbbbbbbbbbbbbbbbbbbbbbb",
        }
        decision = evaluate_admission_policy(
            subject(receipt_refs=[failed_receipt]),
            now="2026-04-30T00:00:00Z",
        )

        self.assertEqual(decision.outcome, "blocked")
        self.assertEqual(decision.reasons[0].code, "validation-receipt-not-successful")

    def test_valid_waiver_records_waived_decision(self) -> None:
        failed_receipt = {
            "digest": "sha256:" + "c" * 64,
            "outcome": "blocked",
            "receipt_id": "control-receipt:cccccccccccccccccccccccc",
        }
        decision = evaluate_admission_policy(
            subject(
                receipt_refs=[failed_receipt],
                waiver={
                    "authority_ref_id": "wgcf-runtime-repo-guidance",
                    "expires_at": "2026-05-01T00:00:00Z",
                    "reason": "operator-approved temporary exception",
                    "waiver_id": "waiver:example",
                },
            ),
            now="2026-04-30T00:00:00Z",
        )

        self.assertEqual(decision.outcome, "waived")
        self.assertEqual(decision.reasons[0].code, "validation-waived")

    def test_policy_decision_ledger_event_links_receipts(self) -> None:
        decision = evaluate_admission_policy(subject(), now="2026-04-30T00:00:00Z")

        event = build_policy_ledger_event(actor="wgcf-test", decision=decision)

        self.assertEqual(event.action, "policy.decision.recorded")
        self.assertEqual(event.outcome, "allow")
        self.assertEqual(event.target, "repo:workspace-governance-control-fabric")
        self.assertEqual(event.receipt_refs[0]["receipt_id"], SUCCESS_RECEIPT["receipt_id"])

    def test_opa_policy_files_exist_as_policy_surface(self) -> None:
        for rel_path in (
            "policies/opa/admission.rego",
            "policies/opa/policy_ledger.rego",
            "policies/opa/validation_blocking.rego",
        ):
            self.assertTrue((REPO_ROOT / rel_path).is_file(), rel_path)
