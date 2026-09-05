"""Intake v2 readiness projections; no mutation or operator-decision authority."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import subprocess
from typing import Any

from .workspace_intake_contracts import COLLECTIONS, IntakeContracts, IntakeSnapshot, digest


def evaluate_intake(
    envelope: dict[str, Any],
    snapshot: IntakeSnapshot,
    contracts: IntakeContracts,
    workspace_root: Path,
) -> dict[str, Any]:
    request, decision = envelope["request"], envelope["decision"]
    target, outcome = request["target"], decision["outcome"]
    kind, name = target["kind"], target["name"]
    register = snapshot.records["intake-register"]
    current = register[COLLECTIONS[kind]].get(name)
    findings: list[dict[str, str]] = []
    observations: dict[str, Any] = {}

    def fail(code: str, message: str, action: str = "correct-request") -> None:
        findings.append({"code": code, "severity": "blocking", "message": message, "next_action": action})

    if envelope["authority_revision"] != snapshot.revision:
        fail("stale-authority", "Refresh the request against current merged authority.", "refresh-authority")
    if target["record_id"] != f"{kind}:{name}" or request["requested_record"]["kind"] != kind:
        fail("target-mismatch", "The entrant identity and record kind must agree.")
    if decision["target"] != target or decision["request_ref"] != {
        "id": request["request_id"], "digest": request["request_digest"],
    }:
        fail("decision-binding-mismatch", "The operator decision must bind this exact request.")
    accepted = decision["operator_acceptance"]
    if outcome["status"] != "allowed" or accepted["state"] != "accepted":
        fail("operator-decision-not-allowed", "Resolve the operator decision before source preparation.", "review-decision")
    elif (
        outcome["classification"] != request["requested_classification"]
        or outcome["owner_route"] != request["owner_route"]
        or outcome["approved_record"] != request["requested_record"]
    ):
        fail("approved-content-mismatch", "The decision must approve the exact requested content.")
    if not request["owner_route"].strip():
        fail("owner-route-missing", "Select the accountable owner route.")
    if not accepted["operator_ref"].strip():
        fail("operator-identity-missing", "The acceptance needs an accountable operator.")
    if not (
        _date(request["requested_at"]) <= _date(decision["decided_at"])
        and _date(request["requested_at"]) <= _date(accepted["recorded_at"])
    ):
        fail("decision-chronology-invalid", "A decision cannot predate its request.")

    all_entries = [
        entry for collection in COLLECTIONS.values() for entry in register[collection].values()
    ]
    used = next((
        entry for entry in all_entries
        if entry["record"]["last_mutation"]["idempotency_key"] == request["idempotency_key"]
    ), None)
    replay = False
    if used is not None:
        mutation = used["record"]["last_mutation"]
        replay = (
            used["record"]["id"] == target["record_id"]
            and mutation["request_digest"] == request["request_digest"]
            and mutation["decision_digest"] == decision["decision_digest"]
        )
        if not replay:
            fail("idempotency-conflict", "This mutation key already identifies different content.", "stop")
    if not replay:
        expected = request["expected_state"]
        if expected["register_digest"] != digest(register):
            fail("stale-register", "Refresh the request against the current intake register.", "refresh-authority")
        if request["action"] == "add":
            if current is not None:
                fail("record-already-exists", "Use an explicit update for an existing intake record.")
            if expected["record_version"] is not None or expected["record_digest"] is not None:
                fail("add-state-invalid", "New records require empty prior record bindings.")
        elif current is None:
            fail("record-not-found", "An update requires an existing intake record.")
        else:
            if expected["record_version"] != current["record"]["version"] or expected["record_digest"] != digest(current):
                fail("stale-record", "Refresh the exact current record version and digest.", "refresh-authority")
            if request["source"] != current["record"]["source"]:
                fail("source-identity-changed", "Updates must preserve the original entrant source.")

    inventory = snapshot.records[COLLECTIONS[kind]]
    occupied = name in inventory.get(COLLECTIONS[kind], {})
    if kind == "repo":
        occupied = occupied or name in inventory.get("retired_repos", {})
    if occupied:
        fail("inventory-overlap", "This entrant already belongs to workspace inventory.", "stop")

    record = request["requested_record"]
    in_scope = request["requested_classification"] in {"proposed", "admitted"}
    if kind == "repo":
        if in_scope and (
            not _present(record["repo_class"])
            or not isinstance(record["requires_security_bindings"], bool)
            or (record["requires_security_bindings"] and not _present(record["security_owner"]))
        ):
            fail("repository-ownership-missing", "Set the repository class and required Security ownership.")
        fields = ("repo_class", "requires_security_bindings", "security_owner")
        if request["requested_classification"] == "admitted" and contracts.policy["repos"]["admitted_requires_physical_repo"]:
            root = workspace_root / name
            required = contracts.policy["repos"]["admitted_required_files"]
            present = {
                filename: (root / filename).is_file() for filename in required
            } if root.resolve().parent == workspace_root.resolve() else {filename: False for filename in required}
            physical = root.resolve().parent == workspace_root.resolve() and _repository_present(root)
            observations["repository_files"] = present
            observations["repository_present"] = physical
            if not physical or not all(present.values()):
                fail("repository-not-present", "Admitted repositories require their owner files in the workspace.", "prepare-repository")
    elif kind == "product":
        fields = ("platform_owner", "security_owner", "runtime_owner", "intended_endpoint")
        if in_scope and (
            any(not _present(record[field]) for field in fields)
            or not record["source_owners"] or any(not _present(owner) for owner in record["source_owners"])
        ):
            fail("product-ownership-missing", "Set platform, Security, runtime, endpoint, and source ownership.")
        if not in_scope and record["source_owners"]:
            fail("out-of-scope-ownership", "Out-of-scope products cannot claim source ownership.")
    else:
        fields = ("component_class", "owner_repo", "security_owner", "product")
        if in_scope and any(not _present(record[field]) for field in fields[:3]):
            fail("component-ownership-missing", "Set the component class and owner responsibilities.")
    if not in_scope and any(record[field] is not None for field in fields):
        fail("out-of-scope-ownership", "Out-of-scope entrants cannot claim governed ownership metadata.")
    behavior = record.get("validation_behavior")
    policy = contracts.policy["validation_behavior"]
    if in_scope and policy[COLLECTIONS[kind]]["require_for_in_scope_intake"] and not behavior:
        fail("validation-behavior-missing", "Declare how the entrant participates in validation.")
    if not in_scope and behavior:
        fail("out-of-scope-validation", "Out-of-scope entrants cannot claim validation participation.")
    if behavior:
        posture = behavior["posture"]
        role = behavior["wgcf_graph_role"]
        allowed_roles = policy["allowed_graph_roles_by_posture"].get(posture, policy["allowed_graph_roles"])
        if posture not in policy["allowed_postures"] or role not in allowed_roles:
            fail("validation-behavior-invalid", "Use a supported validation posture and graph role.")
        if posture in policy["direct_invocation_postures"] and not behavior["catalog_refs"]:
            fail("validation-catalog-missing", "Direct validation requires catalog references.")

    if decision["decision_source"] == "ai-suggested":
        suggestion = decision["ai_suggestion"]
        assist = snapshot.records["governed-intake-assist"].get("governed_intake_assist", {})
        activation, consumer = assist.get("activation_state", {}), assist.get("consumer", {})
        if (
            activation.get("source_contract_status") != "active"
            or activation.get("live_consumption_allowed") is not True
            or suggestion["policy_status"] != "active"
            or any(suggestion[field] != consumer.get(field) for field in ("profile_id", "caller_id", "invocation_path"))
        ):
            fail("ai-profile-not-authorized", "Use the active governed intake-assist profile.", "review-decision")
        classification = outcome["classification"]
        if (
            suggestion["operator_decision"] != classification
            or suggestion["accepted_by"] != accepted["operator_ref"]
            or (suggestion["acceptance_state"] == "accepted" and suggestion["suggested_decision"] != classification)
            or (suggestion["acceptance_state"] == "overridden" and (
                suggestion["suggested_decision"] == classification or not suggestion.get("override_reason")
            ))
        ):
            fail("ai-acceptance-mismatch", "Bind the operator's exact acceptance or override.", "review-decision")
        if not replay and any(
            entry.get("ai_suggestion", {}).get("decision_id") == suggestion["decision_id"]
            for entry in all_entries
        ):
            fail("ai-decision-reused", "This AI decision already belongs to an applied mutation.", "stop")

    return {
        "outcome": ("requires-action" if outcome["status"] == "requires-action" else "denied") if findings else "allowed",
        "next_action": findings[0]["next_action"] if findings else (
            "read-merged-record" if replay else "prepare-reviewed-source-change"
        ),
        "findings": findings,
        "observed_state": {
            "register_digest": digest(register),
            "record_version": current["record"]["version"] if current else None,
            "record_digest": digest(current) if current else None,
            "canonical_replay": replay,
            **observations,
        },
        "obligations": [
            "explicit-operator-acceptance", "review-exact-source-head",
            "human-merge", "merged-authority-readback",
            "no-runtime-activation", "no-active-inventory-promotion",
        ],
    }


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _present(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _repository_present(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        return Path(result.stdout.strip()).resolve() == root.resolve()
    except (OSError, subprocess.SubprocessError):
        return False
