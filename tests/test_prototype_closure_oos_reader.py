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
        operator_id="agent-gary", retirement_reason=None,
    )


def proof() -> dict:
    return {
        "ref": TARGET, "owner_ref": "workspace-delivery-art",
        "digest": "sha256:" + "c" * 64, "state": "accepted",
        "subject_ref": TARGET, "source_revision": None,
        "source_packet_ref": PACKET, "prototype_id": "sample-tool",
    }


def discovered_lookup() -> ClosureEvidenceLookup:
    return ClosureEvidenceLookup(
        field="runtime_disposition_proof_ref", owner_ref="platform-engineering",
        prototype_id="sample-tool", requested_ref=None,
        subject_ref="plan://runtime/sample-tool", source_revision="a" * 40,
        source_packet_ref=None, target_delivery_ref=None,
        accepted_delivery_target_receipt_ref=None, operator_id="agent-gary",
        retirement_reason="Prototype work is no longer active.",
    )


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
                self.assertEqual(body["owner_ref"], "workspace-delivery-art")
                self.assertEqual(body["subject_ref"], TARGET)
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

    def test_owner_discovered_proof_does_not_require_a_caller_supplied_ref(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credential = Path(directory) / "credential"
            credential.write_text("local-test-secret\n")
            value = {
                "ref": "proof://platform/runtime/" + "d" * 64,
                "owner_ref": "platform-engineering",
                "digest": "sha256:" + "d" * 64,
                "state": "accepted",
                "subject_ref": "plan://runtime/sample-tool",
                "source_revision": "a" * 40,
                "source_packet_ref": None,
                "prototype_id": "sample-tool",
            }

            def respond(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content)
                self.assertNotIn("ref", body)
                self.assertEqual(body["subject_ref"], "plan://runtime/sample-tool")
                self.assertEqual(body["operator_id"], "agent-gary")
                return httpx.Response(200, json=value)

            reader = OosClosureOwnerReader(
                base_url="http://127.0.0.1:8111", credential_file=credential,
                client=httpx.Client(transport=httpx.MockTransport(respond)),
            )
            self.assertEqual(reader.read(discovered_lookup()).ref, value["ref"])

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
                reader.read(replace(lookup(), field="retention_plan_ref"))
