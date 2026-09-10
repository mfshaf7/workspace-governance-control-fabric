# Prototype Maturity Readiness Bundle

This directory contains the digest-pinned Workspace Governance contracts used
by WGCF to evaluate Candidate Promotion and Baseline Promotion readiness.

WGCF reads committed Prototype Studio source and produces immutable,
caller-scoped readiness evidence. It does not edit Prototype source, decide or
apply a promotion, grant Delivery or runtime authority, or claim Security
approval. `manifest.json` activates only the WGCF source capability and pins
the merged normal-availability review, composed conformance packet, dedicated
identity packet, and identity definition. Runtime construction still requires
the approved `dev-integration` profile and explicit environment activation.

`source-authority-manifest.json` is the exact Prototype Studio source-contract
manifest pinned by `manifest.json`; it supports deterministic isolated tests
without weakening the committed-source check.
