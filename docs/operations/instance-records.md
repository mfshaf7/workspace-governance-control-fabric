# Internal operational record storage

This unpublished implementation reuses the fabric database for accepted
installation records. Governance owns policy; OOS owns operator workflow.
The primary policy is
[Internal operational records](https://github.com/mfshaf7/workspace-governance/blob/main/docs/instance-operational-records.md).
Its source must land before dependent runtime activation; this link is the
intended durable policy location, not evidence that the pending contract is live.

`InstanceRecordStore` is an internal persistence adapter. It requires a
server-side transition validator, and has no public generic write endpoint.
Supplying an `allowed` flag or a decision digest does not authorize a write.
Operating acceptance remains unavailable until the complete OOS decision,
Governance policy, scoped source consumers and Console route are qualified in
the accepted composition. The new Registry metadata projection carries its own
internal authority; it does not insert private project records into public Git
continuity or require public-Git publication as completion evidence.

The database migration adds a current record, immutable revisions, and complete
command/replay evidence. Apply migrations only through accepted Platform
activation. Do not run a migration on the live database to preview this batch.
Store installation identity explicitly; never derive it from a project URL or
allow a browser to select another installation.

The authenticated read routes are `GET /v1/instance-records/repositories`,
`GET /v1/instance-records/repositories/{name}` and the corresponding `/history`.
They reuse the accepted governance-history reader identity and remain disabled
unless the composition explicitly enables `WGCF_INSTANCE_RECORDS_ENABLED=true`
in `dev-integration` with a stable `WGCF_INSTANCE_ID`. Enabling is a Platform and
Security activation step, not an operator form field. The detail response proves
absence with `found: false` inside a successful installation-bound envelope. A
404 from an older backend is not that proof. An unavailable store returns 503.
The list uses one joined query per page; history bounds both count and bytes and
returns continuation without skipping omitted revisions.

An accepted transition compares the current version and digest, preserves the
immutable provider repository ID, and atomically commits all evidence. Promotion
uses the same record key. An exact retry returns its original result after a
restart, even if a later transition has already completed. A changed command or
caller with the same key is a conflict. Lists and history use bounded keyset
pages; missing or corrupt evidence fails closed.

On interruption, restore the database connection and retry the original request.
Do not generate a replacement idempotency key to hide an uncertain result.
Back up current records, revisions and operations in one consistent database
backup. Integrity failure requires recovery of the original evidence; a public
Git snapshot is not a substitute. This implementation makes no promise to
recover arbitrary hardware corruption or to authenticate a malicious database
administrator from an unkeyed content digest.

Existing source-backed records and reviews retain their original authority and
receipts. No data is automatically imported, exported or published by these
tables. Project source still uses the existing source review controls, and
metadata-reader permissions remain unchanged.


Native request operations (unpublished): repository `/intake` and `/inventory`
acceptance use the OOS-only `accept-instance-decision` role. Their corresponding
`/preparation` reads and `/readback` original-outcome lookups admit the existing
history reader or OOS `read-instance-records` role. A readback never creates a
record or reevaluates policy. It returns the original accepted revision after
later changes; differing original input conflicts.

`GET /v1/instance-records/repositories/{name}/source-authority?operation=...`
projects native source eligibility for bootstrap, configure-verification or
ordinary-source. It uses current native Governance policy and verifies that the
exact record/policy did not change during the read. Positive legacy proof chooses
the old source reader; an unavailable backend never does. Eligibility grants no
credential, checkout, source write or runtime effect.

The equivalent `/v1/instance-records/provider-repositories/github/{id}/source-authority`
route resolves the installation's unique physical repository to its canonical
key, then runs the same current policy. It accepts the same operation selector.
It never derives an alias from a GitHub name or lists all registration records.

The optional Platform source reader uses a distinct caller/secret pair in
`WGCF_INSTANCE_SOURCE_CALLER_ID` and `WGCF_INSTANCE_SOURCE_CALLER_SECRET`.
Both are absent by default. A partial binding, reused caller ID or reused OOS/
reconciler secret fails closed. Its sole permission is
`read-instance-source-authority`: it may use the exact owner eligibility route,
but cannot list/export records, read history/preparations/original commands,
accept a decision or reconcile artifacts. Existing OOS and history-reader
permissions retain their documented scope. Platform owns later credential
commissioning in the accepted composition; source tests do not activate it.

`GET /v1/instance-records/repository-catalog` returns a bounded metadata page,
matching counts and `next_after`. Optional `query` is literal text (128 characters)
and `scope` is all, inventory or eligible. Browser-selected installation identities
remain forbidden. Search filters/counts run in the database; callers must use the
explicit continuation rather than treat the first page as the entire register.


Selected repository status reads accept at most 25 exact ordered names and use
one installation-scoped joined query over the current record, revision and
original accepted operation. The response binds the complete requested identity
list, including missing native records, and returns compact state/version/provider
metadata without descriptions or history. Authentication and ordinary read-only
roles are unchanged; the narrow Platform source reader cannot use this endpoint.
Console uses the same status for Repo Control and primary attention rather than
reopening admission from old custody receipts. Unknown routes and failed reads
remain unavailable, never empty successful registration.

Delivery's existing repository readiness request may bind an
`expected_instance_record` containing the exact canonical authority, record
version/digest and policy reference. This is an expectation, not a grant:
WGCF reevaluates current native ordinary-source eligibility using the configured
installation. The durable receipt retains the actual internal reference and
the accepted source-authority policy URI/digest instead of a fictitious public
repository rule. Foreign or stale expectations cannot become ready; suspended
or unavailable authority blocks fresh issuance. OOS refreshes the decision
before linking. Legacy requests and the Catalog reference shape are unchanged.
This does not migrate Catalog records or activate private Studio source import.
