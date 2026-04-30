package workspace_governance_control_fabric.policy_ledger

default recordable = false

recordable {
  input.decision_id
  input.outcome
  input.target
  input.event_action == "policy.decision.recorded"
}

requires_receipt_link {
  input.validation_required
}

receipt_link_present {
  count(input.receipt_refs) > 0
}
