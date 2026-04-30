# Control Fabric CLI

This app owns the future `wgcf` operator CLI implementation.

Current slice:

- provide the Python CLI entry shell
- support `wgcf status`
- support `wgcf graph query --scope <scope>` for read-only manifest graph
  slices
- support `wgcf plan --scope <scope> --tier <tier>` for deterministic
  validation-plan records
- support `wgcf check --scope <scope> --tier <tier>` for bounded local
  validation execution, compact receipt writing, artifact refs, and ledger
  append
- support `wgcf receipts list` for compact local receipt metadata
- keep all policy meaning pointed at the workspace-governance authority contract

Future slices add source snapshots, readiness decisions, ledger tail/read
surfaces, and decision explanations.
