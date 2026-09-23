"""Read current Prototype Closure evidence from OOS-owned custody."""

from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx

from .prototype_closure_authority import PrototypeClosureUnavailable
from .prototype_closure_evidence import ClosureEvidenceLookup
from .prototype_closure_policy import VerifiedReference


FIELDS = {
    "accepted_baseline_receipt_ref",
    "target_delivery_ref",
    "accepted_delivery_target_receipt_ref",
    "durable_owner_acceptance_ref",
    "source_transfer_receipt_ref",
    "already_owned_source_proof_ref",
    "runtime_disposition_plan_ref",
    "runtime_disposition_proof_ref",
    "prior_retirement_receipt_ref",
}
OWNER_DISCOVERED_FIELDS = {
    "source_transfer_receipt_ref",
    "runtime_disposition_proof_ref",
}
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class OosClosureOwnerReader:
    """Use a caller-specific service credential; never accept proof from WGCF's request."""

    def __init__(
        self, *, base_url: str, credential_file: Path, client: httpx.Client | None = None
    ) -> None:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise PrototypeClosureUnavailable("Closure OOS reader URL is invalid")
        self.url = base_url.rstrip("/") + "/v1/prototype-closures/owner-readbacks"
        self.credential_file = credential_file
        self.client = client or httpx.Client(timeout=5.0, follow_redirects=False)

    def read(self, lookup: ClosureEvidenceLookup) -> VerifiedReference | None:
        if (
            lookup.field not in FIELDS
            or (not lookup.requested_ref and lookup.field not in OWNER_DISCOVERED_FIELDS)
        ):
            raise PrototypeClosureUnavailable("Closure OOS reader does not own this evidence field")
        try:
            secret = self.credential_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise PrototypeClosureUnavailable("Closure OOS reader credential is unavailable") from exc
        if not secret:
            raise PrototypeClosureUnavailable("Closure OOS reader credential is empty")
        payload = {
            "field": lookup.field,
            "owner_ref": lookup.owner_ref,
            "prototype_id": lookup.prototype_id,
            "source_revision": lookup.source_revision,
        }
        if lookup.requested_ref is not None:
            payload["ref"] = lookup.requested_ref
        for field in (
            "subject_ref", "source_packet_ref", "target_delivery_ref",
            "accepted_delivery_target_receipt_ref", "retirement_reason",
        ):
            value = getattr(lookup, field)
            if value is not None:
                payload[field] = value
        payload["operator_id"] = lookup.operator_id
        try:
            response = self.client.post(
                self.url, json=payload, headers={
                    "x-oos-caller-id": "workspace-governance-control-fabric",
                    "x-oos-caller-secret": secret,
                },
            )
        except httpx.HTTPError as exc:
            raise PrototypeClosureUnavailable("Closure OOS owner readback is unavailable") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200 or len(response.content) > 8192:
            raise PrototypeClosureUnavailable("Closure OOS owner readback failed")
        try:
            value = json.loads(response.content)
            required = {
                "ref", "owner_ref", "digest", "state", "subject_ref",
                "source_revision", "source_packet_ref", "prototype_id",
            }
            if not isinstance(value, dict) or set(value) != required:
                raise ValueError("invalid evidence shape")
            if ((lookup.requested_ref is not None and value["ref"] != lookup.requested_ref)
                    or value["owner_ref"] != lookup.owner_ref
                    or value["state"] != "accepted"
                    or not isinstance(value["digest"], str)
                    or not DIGEST.fullmatch(value["digest"])
                    or value["prototype_id"] != lookup.prototype_id
                    or (lookup.subject_ref is not None and value["subject_ref"] != lookup.subject_ref)
                    or (lookup.source_packet_ref is not None
                        and value["source_packet_ref"] != lookup.source_packet_ref)):
                raise ValueError("evidence binding changed")
            return VerifiedReference(**value)
        except (TypeError, ValueError) as exc:
            raise PrototypeClosureUnavailable("Closure OOS owner readback is invalid") from exc
