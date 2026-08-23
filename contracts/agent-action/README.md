# Agent Action Contract Bundle

`manifest.json` pins the Workspace Governance authority and request and
policy-decision schemas at commit
`d6e5a5bf0cac6ddfbf127f5826159556971c3718`. Those files are consumed as
upstream authority and must match their recorded digests.

Files under `fixtures/` are WGCF-local evaluator inputs. The request fixture is
derived from the upstream schema fixture with a computed canonical content
digest; the current-binding fixture represents synthetic runtime truth used by
tests. Neither fixture is workspace policy or an upstream authority artifact.
