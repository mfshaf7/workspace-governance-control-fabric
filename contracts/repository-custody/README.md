# Repository Custody Readiness Contract

This digest-pinned bundle lets WGCF evaluate repository custody and lifecycle
requests against the exact Workspace Governance authority extended by Delivery
`#1050`.

The evaluator issues immutable policy decisions for existing-repository
linking and organization-scoped repository provisioning. An allowed
`provision-new` decision carries the exact approved target and baseline
settings with `create-provider` as the next action. WGCF does not call a
repository provider, change custody, admit a repository, update active
inventory, write Delivery Catalog, or grant runtime activation. OOS owns the
later workflow and provider adapter; Platform owns provider identity; Security
owns trust acceptance.

The lifecycle evaluator keeps custody, provider archive state, and workspace
record state independent. It evaluates transfer, archive, unarchive, retire,
and restore requests against exact state versions, impact disposition,
confirmation references, reversal evidence, and the approved provider
authority. Its decision names one next action and always records
`downstream_mutation: none`.

The copied authority and schemas remain upstream-owned. `manifest.json` binds
their exact source commit and byte digests so local changes fail closed.

Custody readiness uses the existing WGCF source-manifest activation model,
separate from the upstream policy's deliberately fixed `contract-only` flag.
The manifest binds the merged Security controlled-activation review and its
content digest, and admits only `link-existing` and `provision-new`. The normal
factory additionally requires the `dev-integration` profile and explicit
Platform enablement. Missing or invalid review metadata, disabled source
activation and other profiles fail closed. Lifecycle activation is unchanged
and remains denied; this does not activate providers or downstream admission.
