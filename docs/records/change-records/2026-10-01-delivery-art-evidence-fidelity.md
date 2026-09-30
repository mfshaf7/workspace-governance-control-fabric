# Delivery ART Evidence Fidelity

## Summary

- Date: 2026-10-01
- Short title: Cover filesystem and real-git conformance in the owner profile
- Environment: Delivery ART owner evidence acquisition
- Severity: High

## Classification

- Type: owner-repository maintenance
- Signal: `2026-10-01-owner-evidence-fidelity-regression`
- Owner: workspace-governance-control-fabric

## Change

The base-owned evidence profile now binds the isolated owner test runner to
filesystem conformance and the source-diff validator to real-git conformance.
This preserves independent proof for both architecture fidelities and prevents
source work from modifying the evidence profile used to validate itself.

The profile remains read from the admitted base revision. This maintenance
change does not alter Workspace Intake or Active Inventory runtime behavior.

## Validation

The profile test proves both command bindings. The normal Delivery ART evidence
acquisition path remains responsible for executing the commands against an
exact clean pushed source revision.
