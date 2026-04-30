package workspace_governance_control_fabric.admission

default allow = false

allow {
  valid_subject
  count(deny) == 0
  count(block) == 0
  count(review) == 0
}

valid_subject {
  input.subject_type == "repo"
}

valid_subject {
  input.subject_type == "component"
}

deny["unsupported-subject-type"] {
  not valid_subject
}

deny["missing-owner-repo"] {
  not input.owner_repo
}

review["missing-authority-ref"] {
  count(input.authority_refs) == 0
}

block["stale-authority-ref"] {
  ref := input.authority_refs[_]
  lower(ref.freshness_status) != "current"
}

review["missing-validation-receipt"] {
  input.validation_required
  count(input.receipt_refs) == 0
  not valid_waiver
}

block["validation-receipt-not-successful"] {
  receipt := input.receipt_refs[_]
  lower(receipt.outcome) == "failure"
  not valid_waiver
}

block["validation-receipt-not-successful"] {
  receipt := input.receipt_refs[_]
  lower(receipt.outcome) == "blocked"
  not valid_waiver
}

valid_waiver {
  input.waiver.waiver_id
  input.waiver.authority_ref_id
  input.waiver.reason
  input.waiver.expires_at
}
