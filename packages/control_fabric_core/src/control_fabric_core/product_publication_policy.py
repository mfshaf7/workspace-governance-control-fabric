"""Non-mutating Product Publication readiness policy."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .product_publication_contracts import ProductPublicationSnapshot, digest


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

    lifecycle_binding = manifest.get("inventory_lifecycle_binding")
    lifecycle_event: dict[str, Any] | None = None
    if envelope["schema_version"] == 2:
        if manifest.get("schema_version") != 2 or not isinstance(lifecycle_binding, dict):
            find(
                "lifecycle-binding-missing",
                "Schema-v2 evaluation requires schema-v2 owner lifecycle evidence.",
            )
        else:
            event_ref = lifecycle_binding["history_event_ref"]
            target_events = [
                event
                for event in snapshot.history["events"]
                if event["target"]["record_id"] == manifest["product_id"]
            ]
            lifecycle_event = next(
                (event for event in target_events if event["event_id"] == event_ref["id"]),
                None,
            )
            if lifecycle_event is None:
                find(
                    "stale-lifecycle-history",
                    "Owner lifecycle evidence is absent from current committed history.",
                    is_stale=True,
                )
            else:
                if target_events[-1]["event_id"] != lifecycle_event["event_id"]:
                    find(
                        "stale-lifecycle-history",
                        "Owner lifecycle evidence is not the latest committed product event.",
                        is_stale=True,
                    )
                if lifecycle_event["event_digest"] != event_ref["digest"]:
                    find(
                        "lifecycle-event-digest-mismatch",
                        "Owner lifecycle event digest contradicts committed history.",
                    )
                if lifecycle_event["sequence"] != event_ref["sequence"]:
                    find(
                        "lifecycle-event-sequence-mismatch",
                        "Owner lifecycle event sequence contradicts committed history.",
                    )
                for key in ("action", "after", "previous_event_ref"):
                    if lifecycle_event[key] != lifecycle_binding[key]:
                        find(
                            f"lifecycle-{key.replace('_', '-')}-mismatch",
                            f"Owner lifecycle {key.replace('_', ' ')} contradicts committed history.",
                        )
                expected_receipt_ids = {
                    f"workspace-inventory-lifecycle-receipt:{lifecycle_event['idempotency_key']}:review-branch",
                    f"workspace-inventory-lifecycle-receipt:{lifecycle_event['idempotency_key']}:replay",
                }
                if lifecycle_binding["receipt_ref"]["id"] not in expected_receipt_ids:
                    find(
                        "lifecycle-receipt-mismatch",
                        "Owner lifecycle receipt does not identify the committed lifecycle operation.",
                    )

            if isinstance(product, dict):
                after = lifecycle_binding["after"]
                if after["record_version"] != product.get("record", {}).get("version"):
                    find(
                        "lifecycle-record-version-mismatch",
                        "Owner lifecycle record version differs from active inventory.",
                        is_stale=True,
                    )
                if after["record_digest"] != digest(product):
                    find(
                        "lifecycle-record-digest-mismatch",
                        "Owner lifecycle record digest differs from active inventory.",
                        is_stale=True,
                    )
                if after["posture"] != product.get("posture"):
                    find(
                        "lifecycle-record-posture-mismatch",
                        "Owner lifecycle posture differs from active inventory.",
                        is_stale=True,
                    )

    current = manifest["lifecycle_evidence"]["publication_posture"]
    if request["action"] != "restore" and request["current_publication_posture"] != current:
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

    if envelope["schema_version"] == 2 and request["action"] == "archive":
        if not isinstance(lifecycle_binding, dict) or lifecycle_binding.get("action") != "retire":
            find(
                "archive-lifecycle-transition-mismatch",
                "Archive requires the exact latest owner retirement event.",
            )
    if request["action"] == "restore":
        if not isinstance(lifecycle_binding, dict) or lifecycle_binding.get("action") != "restore":
            find(
                "restore-lifecycle-transition-mismatch",
                "Restore requires the exact latest owner restore event.",
            )
        if manifest.get("lifecycle_state") != "active" or current != "unlisted":
            find(
                "restore-owner-state-mismatch",
                "Restore requires active owner lifecycle evidence at unlisted publication posture.",
            )
        previous_event_ref = (
            lifecycle_binding.get("previous_event_ref")
            if isinstance(lifecycle_binding, dict)
            else None
        )
        previous_event = next(
            (
                event
                for event in snapshot.history["events"]
                if previous_event_ref is not None
                and event["event_id"] == previous_event_ref["id"]
            ),
            None,
        )
        if (
            previous_event is None
            or previous_event["event_digest"] != previous_event_ref["digest"]
            or previous_event["target"]["record_id"] != manifest["product_id"]
            or previous_event["action"] not in {"suspend", "retire"}
            or previous_event["after"]["posture"] not in {"suspended", "retired"}
        ):
            find(
                "restore-previous-event-mismatch",
                "Restore requires the exact prior suspension or retirement event.",
            )

    previous = request["previous_receipt"]
    expected_version = request["expected_publication_version"]
    if expected_version == 0 and previous is not None:
        find("unexpected-previous-receipt", "Initial publication cannot bind a previous receipt.")
    if expected_version > 0 and (
        previous is None or previous["publication_version"] != expected_version
    ):
        find("publication-version-conflict", "Expected publication version does not match the previous receipt.", is_stale=True)
    if request["action"] == "restore" and (
        previous is None or previous.get("publication_posture") != "archived"
    ):
        find(
            "restore-previous-receipt-mismatch",
            "Restore requires the exact previous archived publication receipt.",
        )
    requested_at = datetime.fromisoformat(request["requested_at"].replace("Z", "+00:00"))
    if requested_at > evaluated_at:
        find("future-request", "Publication request timestamp is ahead of the evaluation clock.")

    return {
        "outcome": "stale" if stale else ("blocked" if findings else "ready"),
        "findings": findings,
    }
