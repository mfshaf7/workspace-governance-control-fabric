package workspace_governance_control_fabric.validation_blocking

default blocked = false

blocked {
  missing_required_receipt
}

blocked {
  failed_required_receipt
}

missing_required_receipt {
  input.validation_required
  count(input.receipt_refs) == 0
  not input.waiver.waiver_id
}

failed_required_receipt {
  receipt := input.receipt_refs[_]
  lower(receipt.outcome) == "failure"
  not input.waiver.waiver_id
}

failed_required_receipt {
  receipt := input.receipt_refs[_]
  lower(receipt.outcome) == "blocked"
  not input.waiver.waiver_id
}
