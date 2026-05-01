# Control Fabric CLI

This app owns the future `wgcf` operator CLI implementation.

Current slice:

- provide the Python CLI entry shell
- support `wgcf status`
- support `wgcf graph query --scope <scope>` for read-only manifest graph
  slices with budgeted pagination
- support `wgcf sources snapshot` for compact source snapshot status without
  raw authority content or full digest maps
- support `wgcf plan --scope <scope> --tier <tier>` for deterministic
  validation-plan records
- support `wgcf check --scope <scope> --tier <tier>` for bounded local
  validation execution, compact receipt writing, artifact refs, and ledger
  append
- support `wgcf catalog plan` and `wgcf catalog check` for catalog-backed
  validator invocation from the workspace-governance authority catalog,
  including selected/suppressed entry reporting and compact receipts
- support `wgcf receipts list` for compact local receipt metadata
- support `wgcf inspect --receipt <receipt-id-or-path>` for compact receipt
  inspection without reading raw artifacts or leaving the receipt directory
- support `wgcf readiness --target <target> --profile <profile>` for local
  readiness decisions with a fabric-local ledger event
- support `wgcf budget show` for operator-visible invocation-class budgets
  such as `inline-fast`, `receipt-check`, `hard-gate`, and `checkpoint-batch`
- support `wgcf lifecycle plan` and `wgcf lifecycle apply --confirm` for
  fabric-local artifact cleanup, receipt retention, ledger export, and ledger
  compaction
- support `wgcf metrics receipts` for compact receipt metrics and correlation
  visibility without opening raw artifacts
- support `wgcf art graph --context <broker-context.json>` for compact
  broker-owned ART context graph projection
- support `wgcf art readiness --context <broker-context.json>` for
  pre-mutation ART readiness receipts and OOS route recommendations
- support `wgcf art evidence --receipt <receipt.json>` for compact ART
  completion and Review Packet evidence projected from WGCF receipts
- keep all policy meaning pointed at the workspace-governance authority contract

Future slices add ledger tail/read surfaces and decision explanations.
