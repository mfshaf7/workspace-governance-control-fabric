# Prototype Ingress Readiness Contracts

This bundle lets WGCF evaluate one committed Prototype Delivery packet before
OOS may apply it to Workspace Delivery ART.

- `prototype-delivery-packet.schema.json` is an exact digest-pinned snapshot of
  the landed Workspace Prototype Studio packet schema.
- `prototype-ingress-readiness-request.schema.json` defines the bounded API
  request.
- `prototype-ingress-readiness-receipt.schema.json` defines the immutable
  allow/deny receipt consumed by OOS.

WGCF verifies the packet and its Git/baseline/source projection, but does not
mutate Prototype or Delivery state. An `allow` receipt is only an eligibility
input to the separately governed OOS target-application workflow.
