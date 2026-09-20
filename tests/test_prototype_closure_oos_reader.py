from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import TestCase

import httpx

from control_fabric_core.prototype_closure_authority import PrototypeClosureUnavailable
from control_fabric_core.prototype_closure_evidence import ClosureEvidenceLookup
from control_fabric_core.prototype_closure_oos_reader import OosClosureOwnerReader


RECEIPT = "oos://receipts/prototype-delivery-application/" + "b" * 64
PACKET = "record://delivery-packets/sample-tool"
TARGET = "openproject://work_packages/100"


def lookup() -> ClosureEvidenceLookup:
    return ClosureEvidenceLookup(
        field="target_delivery_ref", owner_ref="workspace-delivery-art",
        prototype_id="sample-tool", requested_ref=TARGET, subject_ref=TARGET,
        source_revision="a" * 40, source_packet_ref=PACKET,
        target_delivery_ref=TARGET, accepted_delivery_target_receipt_ref=RECEIPT,
    )


def proof() -> dict:
    return {
        "ref": TARGET, "owner_ref": "workspace-delivery-art",
        "digest": "sha256:" + "c" * 64, "state": "accepted",
        "subject_ref": None, "source_revision": None,
        "source_packet_ref": PACKET, "prototype_id": "sample-tool",
    }


class OosClosureOwnerReaderTests(TestCase):
    def test_exact_bound_read_uses_dedicated_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "credential"
            credential.write_text("local-test-secret\n")

            def respond(request: httpx.Request) -> httpx.Response:
                self.assertEqual(request.url.path, "/v1/prototype-closures/owner-readbacks")
                self.assertEqual(request.headers["x-oos-caller-id"], "workspace-governance-control-fabric")
                self.assertEqual(request.headers["x-oos-caller-secret"], "local-test-secret")
                body = json.loads(request.content)
                self.assertEqual(body["source_packet_ref"], PACKET)
                self.assertEqual(body["accepted_delivery_target_receipt_ref"], RECEIPT)
                return httpx.Response(200, json=proof())

            client = httpx.Client(transport=httpx.MockTransport(respond))
            reader = OosClosureOwnerReader(
                base_url="http://127.0.0.1:8111", credential_file=credential, client=client,
            )
            result = reader.read(lookup())
            self.assertEqual(result.ref, TARGET)
            self.assertEqual(result.source_packet_ref, PACKET)

    def test_wrong_owner_or_unavailable_readback_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "credential"
            credential.write_text("local-test-secret")
            for status, body in [
                (200, {**proof(), "owner_ref": "operator-orchestration-service"}),
                (503, {"error": "inactive"}),
            ]:
                client = httpx.Client(transport=httpx.MockTransport(
                    lambda _request: httpx.Response(status, json=body)
                ))
                reader = OosClosureOwnerReader(
                    base_url="http://127.0.0.1:8111", credential_file=credential, client=client,
                )
                with self.assertRaises(PrototypeClosureUnavailable):
                    reader.read(lookup())
            missing = httpx.Client(transport=httpx.MockTransport(
                lambda _request: httpx.Response(404)
            ))
            reader = OosClosureOwnerReader(
                base_url="http://127.0.0.1:8111", credential_file=credential, client=missing,
            )
            self.assertIsNone(reader.read(lookup()))

    def test_reader_does_not_use_caller_proof_or_unsupported_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "credential"
            credential.write_text("local-test-secret")
            reader = OosClosureOwnerReader(
                base_url="http://127.0.0.1:8111", credential_file=credential,
                client=httpx.Client(transport=httpx.MockTransport(
                    lambda _request: self.fail("unexpected HTTP read")
                )),
            )
            from dataclasses import replace
            with self.assertRaises(PrototypeClosureUnavailable):
                reader.read(replace(lookup(), field="prior_retirement_receipt_ref"))
