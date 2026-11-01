"""Read-only publication input preparation from registered, committed owner source."""

from __future__ import annotations

from datetime import timedelta
import re
from typing import Any

from .product_publication_contracts import (
    COMMIT_PATTERN, REPO_PATTERN, ProductPublicationRequestError,
    ProductPublicationUnavailable, digest, parse_json, yaml_record,
)
from .record_authority_freshness import require_record_authority_freshness


def prepare_publication(authority: Any, product_id: str, *, now: Any, issuer: dict) -> dict:
    if not re.fullmatch(r"product:[a-z0-9][a-z0-9._-]*", product_id):
        raise ProductPublicationRequestError("Product identity is invalid")
    revision = authority._commit(authority.workspace_root, authority.trusted_ref)
    require_record_authority_freshness(authority.workspace_root, "workspace-governance", revision,
        trusted_ref=authority.trusted_ref, unavailable=ProductPublicationUnavailable)
    products = yaml_record(authority._read(authority.workspace_root, revision, "contracts/products.yaml"))
    product = products.get("products", {}).get(product_id[8:])
    if not isinstance(product, dict) or product.get("record", {}).get("id") != product_id:
        raise ProductPublicationRequestError("Select a registered product")
    owners = product.get("source_owners", [])
    if not owners or len(owners) > 8 or any(not isinstance(owner, str) or not REPO_PATTERN.fullmatch(owner) for owner in owners):
        raise ProductPublicationUnavailable("Product source ownership is unavailable")

    candidates, invalid = [], False
    for owner in owners:
        root = authority.owner_roots.get(owner)
        if root is None:
            root = authority._registered_owner_root(owner, products, revision)
        head = authority._commit(root, authority.trusted_ref)
        matches = authority._git(root, "grep", "-I", "-lz", "-F", "--no-textconv",
            "product-publication-owner-manifest", head, "--", allow_failure=True) or b""
        names = [name for name in matches.split(b"\0") if name]
        if len(names) > 64:
            raise ProductPublicationUnavailable("Publication discovery exceeds the bounded source limit")
        for name in names:
            prefix = (head + ":").encode()
            if not name.startswith(prefix):
                raise ProductPublicationUnavailable("Publication source identity is invalid")
            path = name[len(prefix):].decode("utf-8")
            raw = authority._read(root, head, path)
            if not raw.lstrip().startswith(b"{"):
                continue
            try:
                manifest = parse_json(raw)
                if manifest.get("artifact_type") != "product-publication-owner-manifest" or manifest.get("product_id") != product_id:
                    continue
                source_revision = manifest.get("owner_revision", "")
                if manifest.get("owner_repo") != owner or not COMMIT_PATTERN.fullmatch(source_revision):
                    raise ProductPublicationRequestError("Owner identity or revision is invalid")
                locator = {"repo": owner, "path": path, "source_revision": source_revision,
                    "manifest_revision": head, "manifest_digest": digest(manifest)}
                snapshot = authority.snapshot(locator)
                if snapshot.authority_revision != revision or snapshot.owner_head != head:
                    raise ProductPublicationUnavailable("Publication source changed; load it again")
                if snapshot.manifest["product_id"] != product_id or snapshot.manifest_digest != locator["manifest_digest"]:
                    raise ProductPublicationUnavailable("Publication source does not match the selected product")
                candidates.append((locator, snapshot.manifest["schema_version"], snapshot.manifest["lifecycle_evidence"]["publication_posture"]))
            except ProductPublicationRequestError:
                invalid = True

    issues = []
    if len(candidates) > 1:
        issues.append({"code": "ambiguous-owner-information", "message": "More than one source owner provides publication information. The product owners must identify the authoritative publication file."})
    elif not candidates:
        issues.append({"code": "invalid-owner-information" if invalid else "missing-owner-information",
            "message": "The product owner needs to correct its publication information." if invalid else "The product owner has not prepared publication information yet."})
    return {"schema_version": 1, "product_id": product_id, "authority_revision": revision,
        "status": "prepared" if len(candidates) == 1 and not issues else "blocked",
        "manifest_source": candidates[0][0] if len(candidates) == 1 and not issues else None,
        "manifest_schema_version": candidates[0][1] if len(candidates) == 1 and not issues else None,
        "manifest_publication_posture": candidates[0][2] if len(candidates) == 1 and not issues else None,
        "owner_repos": owners, "issues": issues,
        "prepared_at": now.isoformat(), "expires_at": (now + timedelta(minutes=2)).isoformat(),
        "issuer": issuer, "publication_authority_granted": False}
