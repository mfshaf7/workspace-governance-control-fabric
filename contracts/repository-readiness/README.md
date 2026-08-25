# Repository Readiness Contract

This bundle defines the bounded WGCF decision used before Delivery Catalog links
an admitted workspace repository as an Owner Repo value.

WGCF reads the exact `workspace-governance/contracts/repos.yaml` version named
by the request and the repository's matching rule. It emits an immutable
readiness receipt and an OOS-compatible reference. It does not create, admit,
retire, rename, or mutate a repository, and it does not write Catalog state.

The supported outcomes are:

- `ready`
- `not_admitted`
- `retired`
- `stale`
- `contract_mismatch`

