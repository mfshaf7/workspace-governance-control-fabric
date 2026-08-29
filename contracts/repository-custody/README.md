# Repository Custody Readiness Contract

This digest-pinned bundle lets WGCF evaluate one repository custody request
against the exact Workspace Governance authority merged for Delivery `#1040`.

The evaluator issues an immutable policy decision for the first active
capability, `link-existing`. It does not call a repository provider, change
custody, admit a repository, update active inventory, write Delivery Catalog,
or grant runtime activation. OOS owns the later workflow and provider adapter;
Platform owns provider identity; Security owns trust acceptance.

The copied authority and schemas remain upstream-owned. `manifest.json` binds
their exact source commit and byte digests so local changes fail closed.
