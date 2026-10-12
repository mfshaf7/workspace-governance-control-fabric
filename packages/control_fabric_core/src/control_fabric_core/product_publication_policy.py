"""Non-mutating Product Publication readiness policy."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .product_publication_contracts import ProductPublicationSnapshot


def evaluate_product_publication(
    envelope: dict[str, Any],
    snapshot: ProductPublicationSnapshot,
    evaluated_at: datetime,
) -> dict[str, Any]:
    source = envelope["manifest_source"]
    request = envelope["request"]
    manifest = snapshot.manifest
    model = snapshot.policy["product_publication"]
    findings: list[dict[str, str]] = []
    stale = False

    def find(code: str, message: str, *, is_stale: bool = False) -> None:
        nonlocal stale
        stale = stale or is_stale
        findings.append({"code": code, "severity": "blocking", "message": message})

    if envelope["authority_revision"] != snapshot.authority_revision:
        find("stale-authority", "Refresh against the current merged Workspace Governance revision.", is_stale=True)
    if source["manifest_revision"] != snapshot.owner_head:
        find("stale-owner-manifest", "Refresh the manifest from the current merged owner revision.", is_stale=True)
    if source["manifest_digest"] != snapshot.manifest_digest:
        find("manifest-digest-mismatch", "Committed and submitted manifest digests differ.")
    if source["repo"] != manifest["owner_repo"]:
        find("manifest-owner-mismatch", "Manifest source repository does not match owner_repo.")
    if source["source_revision"] != manifest["owner_revision"]:
        find("source-revision-mismatch", "Manifest source revision does not match owner_revision.")

    product_name = manifest["product_id"].removeprefix("product:")
    product = snapshot.products.get("products", {}).get(product_name)
    if not isinstance(product, dict):
        find("product-not-active", "Product is absent from active Workspace Governance inventory.")
    else:
        record = product.get("record", {})
        expected_ref = f"workspace-governance://products/{product_name}/v{record.get('version')}"
        if manifest["product_record_ref"] != expected_ref or manifest["product_record_version"] != record.get("version"):
            find("stale-product-record", "Manifest does not bind the current product record version.", is_stale=True)
        if manifest["product_classification"] != product.get("maturity"):
            find("classification-mismatch", "Manifest classification differs from active product inventory.")
        if manifest["lifecycle_state"] != product.get("posture"):
            find("lifecycle-mismatch", "Manifest lifecycle differs from active product inventory.")
        if manifest["owner_role"] == "runtime-owner" and manifest["owner_repo"] != product.get("runtime_owner"):
            find("owner-not-authorized", "Manifest repository is not the product runtime owner.")
        if manifest["owner_role"] == "source-owner" and manifest["owner_repo"] not in product.get("source_owners", []):
            find("owner-not-authorized", "Manifest repository is not a declared product source owner.")

    if not any(
        ref.get("repo") == manifest["owner_repo"]
        and ref.get("revision") == manifest["owner_revision"]
        for ref in manifest["source_refs"]
    ):
        find("owner-source-ref-missing", "Manifest does not bind its owner source revision.")

    current = manifest["lifecycle_evidence"]["publication_posture"]
    if request["current_publication_posture"] != current:
        find("current-posture-mismatch", "Request current posture differs from owner lifecycle evidence.", is_stale=True)
    action = model["action_bindings"].get(request["action"])
    target = request["target_publication"]
    if action is None:
        find("unsupported-action", "Publication action is not supported.")
    else:
        if request["current_publication_posture"] not in action["allowed_source_postures"]:
            find("invalid-source-posture", "Publication action is invalid for the current posture.")
        if target["posture"] not in action["allowed_target_postures"]:
            find("invalid-target-posture", "Publication action is invalid for the target posture.")
    posture = model["posture_bindings"].get(target["posture"])
    if posture is None:
        find("unsupported-target-posture", "Target publication posture is unsupported.")
    else:
        for key in ("listing_scope", "audience", "access_class"):
            if target[key] != posture[key]:
                find(f"target-{key}-mismatch", f"Target {key} contradicts the publication posture.")
        if manifest["lifecycle_state"] not in posture["allowed_lifecycle_states"]:
            find("target-lifecycle-mismatch", "Target posture contradicts the product lifecycle.")

    evidence = manifest["lifecycle_evidence"]
    if target["posture"] in {"internal", "client", "public"}:
        if evidence["project_phase"] != "operational":
            find("project-not-operational", "Listed publication requires an operational project.")
        if evidence["source_custody"] not in {"dedicated-owner-repo", "shared-owner-repo"}:
            find("source-custody-not-durable", "Listed publication requires durable source custody.")
        if evidence["release_posture"] not in {"not-applicable", "released"}:
            find("release-not-ready", "Listed publication requires not-applicable or released posture.")
    if target["posture"] == "archived" and evidence["project_phase"] != "retired":
        find("archive-project-not-retired", "Archived publication requires a retired project.")

    previous = request["previous_receipt"]
    expected_version = request["expected_publication_version"]
    if expected_version == 0 and previous is not None:
        find("unexpected-previous-receipt", "Initial publication cannot bind a previous receipt.")
    if expected_version > 0 and (
        previous is None or previous["publication_version"] != expected_version
    ):
        find("publication-version-conflict", "Expected publication version does not match the previous receipt.", is_stale=True)
    requested_at = datetime.fromisoformat(request["requested_at"].replace("Z", "+00:00"))
    if requested_at > evaluated_at:
        find("future-request", "Publication request timestamp is ahead of the evaluation clock.")

    return {
        "outcome": "stale" if stale else ("blocked" if findings else "ready"),
        "findings": findings,
    }
