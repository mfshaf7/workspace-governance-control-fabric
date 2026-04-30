# Control Fabric CLI

This app owns the future `wgcf` operator CLI implementation.

Current slice:

- provide the Python CLI entry shell
- support `wgcf status`
- support `wgcf graph query --scope <scope>` for read-only manifest graph
  slices
- keep all policy meaning pointed at the workspace-governance authority contract

Future slices add source snapshots, validation planning, receipt inspection,
readiness decisions, ledger reads, and decision explanations.
