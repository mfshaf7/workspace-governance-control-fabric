from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core import (  # noqa: E402
    build_governance_record_ledger_event,
    record_approval_decision,
    record_blocker_decision,
    record_change_event,
    record_risk_posture,
    record_waiver_decision,
)


AUTHORITY_REF = {
    "authority_id": "workspace-governance:blocker-contract",
    "digest": "sha256:authority",
    "ref": "main",
}

RECEIPT_REF = {
    "digest": "sha256:" + "a" * 64,
    "outcome": "success",
    "receipt_id": "control-receipt:aaaaaaaaaaaaaaaaaaaaaaaa",
}


class RuntimeGovernanceRecordTests(TestCase):
    def test_blocker_decision_records_decision_path_and_ledger_event(self) -> None:
        record = record_blocker_decision(
            blocker_owner="Workspace Governance Control Fabric",
            decision_path="remove",
            impact="prevents safe continuation",
            next_required_action="land durable control",
            owner_repo="workspace-governance-control-fabric",
            statement="local evidence path is unsafe",
            target="work-item-456",
            authority_refs=[AUTHORITY_REF],
            evidence_refs=[RECEIPT_REF],
            now="2026-04-30T00:00:00Z",
        )
        event = build_governance_record_ledger_event(actor="wgcf-test", record=record)

        self.assertEqual(record.record_type, "blocker_decision")
        self.assertEqual(record.decision, "remove")
        self.assertEqual(record.authority_boundary, "record-only-not-authority")
        self.assertEqual(event.action, "governance.blocker.recorded")
        self.assertEqual(event.outcome, "remove")
        self.assertEqual(event.receipt_refs[0]["receipt_id"], RECEIPT_REF["receipt_id"])

    def test_approval_requires_authority_ref_and_preserves_record_only_boundary(self) -> None:
        record = record_approval_decision(
            approval_ref="security-architecture/reviews/example",
            approver="Security Architecture",
            authority_refs=[AUTHORITY_REF],
            decision="approved",
            owner_repo="workspace-governance-control-fabric",
            target="component:control-fabric-core",
            evidence_refs=[RECEIPT_REF],
            now="2026-04-30T00:00:00Z",
        )

        self.assertEqual(record.record_type, "approval_decision")
        self.assertEqual(record.decision, "approved")
        self.assertEqual(record.authority_boundary, "record-only-not-authority")
        self.assertEqual(record.details["approver"], "Security Architecture")

    def test_approval_without_authority_ref_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            record_approval_decision(
                approval_ref="missing-authority",
                approver="Security Architecture",
                authority_refs=[],
                decision="approved",
                owner_repo="workspace-governance-control-fabric",
                target="component:control-fabric-core",
                now="2026-04-30T00:00:00Z",
            )

    def test_expired_waiver_records_next_required_action(self) -> None:
        record = record_waiver_decision(
            authority_refs=[AUTHORITY_REF],
            expires_at="2026-04-29T00:00:00Z",
            owner_repo="workspace-governance-control-fabric",
            reason="temporary exception expired",
            target="repo:workspace-governance-control-fabric",
            waiver_id="waiver:expired",
            now="2026-04-30T00:00:00Z",
        )

        self.assertEqual(record.record_type, "waiver_decision")
        self.assertEqual(record.decision, "expired")
        self.assertEqual(record.next_required_action, "refresh waiver or remove the exception")

    def test_risk_posture_records_roam_state_without_accepting_risk_locally(self) -> None:
        record = record_risk_posture(
            owner_repo="workspace-governance-control-fabric",
            risk_owner="Platform Architecture",
            risk_ref="openproject://work_packages/999",
            roam_state="mitigated",
            target="delivery-420",
            authority_refs=[AUTHORITY_REF],
            evidence_refs=[RECEIPT_REF],
            now="2026-04-30T00:00:00Z",
        )

        self.assertEqual(record.record_type, "risk_posture")
        self.assertEqual(record.decision, "mitigated")
        self.assertEqual(record.authority_boundary, "record-only-not-authority")

    def test_change_event_requires_evidence_refs_and_changed_surfaces(self) -> None:
        record = record_change_event(
            changed_surfaces=["packages/control_fabric_core/src/control_fabric_core/runtime_governance_records.py"],
            evidence_refs=[RECEIPT_REF],
            owner_repo="workspace-governance-control-fabric",
            record_ref="docs/records/change-records/example.md",
            target="work-item-458",
            authority_refs=[AUTHORITY_REF],
            now="2026-04-30T00:00:00Z",
        )

        self.assertEqual(record.record_type, "change_record")
        self.assertEqual(record.decision, "recorded")
        self.assertEqual(record.details["changed_surfaces"][0], "packages/control_fabric_core/src/control_fabric_core/runtime_governance_records.py")

    def test_runtime_governance_schema_file_exists(self) -> None:
        self.assertTrue((REPO_ROOT / "schemas/runtime-governance-record.schema.json").is_file())
