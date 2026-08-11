from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import sys
from pathlib import Path
from unittest import TestCase

from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core.artifact_registry import (
    ArtifactRegistryAuthorizer,
    ArtifactRegistryConflict,
    ArtifactRegistryContractError,
    ArtifactRegistryForbidden,
    ArtifactRegistryUnauthorized,
    ArtifactRegistryUnavailable,
    DeliveryArtifactRegistry,
    prepare_artifact_registration,
)
from control_fabric_core.artifact_storage import (
    ArtifactStorageSettings,
    ArtifactStorageIntegrityError,
    ArtifactStorageUnavailable,
    S3ArtifactObjectStore,
    StoredArtifactObject,
    delivery_art_object_key,
)
from control_fabric_core.canonical_json import canonical_digest, canonical_json_bytes
from control_fabric_core.db import metadata
from control_fabric_core.db.models import (
    DeliveryArtifactCustodyReceipt,
    LedgerEvent,
)


IMPLEMENTATION_REF = "a" * 40
SERVICE_IDENTITY_REF = (
    "kubernetes://devint-governance-control-fabric/"
    "serviceaccount/workspace-governance-control-fabric-api"
)
FIXED_TIME = datetime(2026, 8, 12, 1, 2, 3, tzinfo=timezone.utc)


class FakeVersionedArtifactStore:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, bytes]] = {}
        self.latest: dict[str, str] = {}
        self.write_count = 0

    def ensure_content(self, content_digest: str, body: bytes) -> StoredArtifactObject:
        if canonical_digest(json.loads(body)) != content_digest:
            raise ArtifactStorageIntegrityError("fake store received bytes with another digest")
        object_key = delivery_art_object_key(content_digest)
        current_version = self.latest.get(object_key)
        if current_version is not None:
            current_body = self.objects[object_key][current_version]
            if current_body != body:
                raise ArtifactStorageIntegrityError("content-addressed key collision")
            return StoredArtifactObject(object_key, current_version, content_digest)
        self.write_count += 1
        version_id = f"version-{self.write_count}"
        self.objects.setdefault(object_key, {})[version_id] = body
        self.latest[object_key] = version_id
        return StoredArtifactObject(object_key, version_id, content_digest)

    def read_version(self, object_key: str, version_id: str) -> bytes:
        return self.objects[object_key][version_id]


def artifact_content(
    *,
    artifact_id: str = "architecture-packet:delivery-810-v1",
    summary: str = "Approved custody architecture.",
    supersedes: dict[str, str] | None = None,
) -> dict:
    content = {
        "schema_version": 1,
        "artifact_type": "delivery_art_architecture_packet",
        "artifact_id": artifact_id,
        "delivery_id": "delivery-698",
        "summary": summary,
        "integrity": {
            "canonicalization": "RFC8785",
            "algorithm": "sha256",
        },
    }
    if supersedes is not None:
        content["custody"] = {"supersedes": supersedes}
    return content


def registration_request(content: dict) -> bytes:
    return canonical_json_bytes(
        {
            "artifact_content": content,
            "content_digest": canonical_digest(content),
        },
    )


class ArtifactRegistryTests(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.storage = FakeVersionedArtifactStore()
        self.registry = DeliveryArtifactRegistry(
            session_factory=self.sessions,
            storage=self.storage,
            service_identity_ref=SERVICE_IDENTITY_REF,
            implementation_ref=IMPLEMENTATION_REF,
            clock=lambda: FIXED_TIME,
        )

    def test_register_read_and_reconcile_return_opaque_durable_references(self) -> None:
        content = artifact_content()
        digest = canonical_digest(content)

        created = self.registry.register(registration_request(content), actor="oos")
        read = self.registry.read(digest, actor="oos")
        reconciled = self.registry.reconcile(digest, actor="wgcf")

        self.assertEqual(created.resolution, "created")
        self.assertEqual(read.resolution, "read")
        self.assertEqual(created.generation, 1)
        self.assertEqual(created.artifact["integrity"]["content_digest"], digest)
        self.assertEqual(created.artifact["custody"]["state"], "durable")
        self.assertEqual(reconciled.artifact_ref, created.to_record()["registry"]["artifact_ref"])
        self.assertNotIn("object_key", json.dumps(created.to_record(), sort_keys=True))
        self.assertNotIn("version-", json.dumps(created.to_record(), sort_keys=True))
        self.assertFalse(hasattr(self.registry, "delete"))

        receipt = created.custody_receipt
        self.assertEqual(receipt["subject"]["content_digest"], digest)
        self.assertEqual(receipt["issuer"]["implementation_ref"], IMPLEMENTATION_REF)
        self.assertEqual(receipt["custody"]["supersedes"], None)
        self.assertTrue(receipt["storage"]["receipt_ref"].startswith("platform-storage://receipts/"))

        with self.sessions() as session:
            actions = list(session.scalars(select(LedgerEvent.action).order_by(LedgerEvent.action)))
        self.assertEqual(
            actions,
            [
                "artifact.registry.persisted",
                "artifact.registry.read",
                "artifact.registry.reconciled",
            ],
        )

    def test_duplicate_digest_reuses_one_object_and_one_receipt(self) -> None:
        request = registration_request(artifact_content())

        first = self.registry.register(request, actor="oos")
        second = self.registry.register(request, actor="oos")

        self.assertEqual(second.resolution, "reused")
        self.assertEqual(first.custody_receipt, second.custody_receipt)
        self.assertEqual(self.storage.write_count, 1)
        with self.sessions() as session:
            actions = list(session.scalars(select(LedgerEvent.action)))
        self.assertEqual(actions.count("artifact.registry.persisted"), 1)
        self.assertEqual(actions.count("artifact.registry.reused"), 1)

    def test_correction_must_supersede_exact_latest_same_subject(self) -> None:
        first = self.registry.register(registration_request(artifact_content()), actor="oos")
        predecessor = first.to_record()["registry"]["artifact_ref"]
        corrected_content = artifact_content(
            summary="Approved custody architecture with correction.",
            supersedes=predecessor,
        )
        corrected = self.registry.register(registration_request(corrected_content), actor="oos")

        self.assertEqual(corrected.generation, 2)
        self.assertEqual(corrected.artifact["custody"]["supersedes"], predecessor)
        self.assertEqual(
            corrected.custody_receipt["custody"]["supersedes"],
            first.to_record()["registry"]["custody_receipt_ref"],
        )
        stale_content = artifact_content(
            summary="A second correction against stale evidence.",
            supersedes=predecessor,
        )
        with self.assertRaisesRegex(ArtifactRegistryConflict, "latest"):
            self.registry.register(registration_request(stale_content), actor="oos")

        cross_subject = artifact_content(
            artifact_id="architecture-packet:delivery-810-other",
            supersedes=corrected.to_record()["registry"]["artifact_ref"],
        )
        with self.assertRaisesRegex(ArtifactRegistryConflict, "first artifact generation"):
            self.registry.register(registration_request(cross_subject), actor="oos")

    def test_changed_subject_requires_explicit_supersession(self) -> None:
        self.registry.register(registration_request(artifact_content()), actor="oos")

        with self.assertRaisesRegex(ArtifactRegistryConflict, "must supersede"):
            self.registry.register(
                registration_request(artifact_content(summary="Changed without lineage.")),
                actor="oos",
            )

    def test_contract_rejects_unsupported_generated_and_mismatched_content(self) -> None:
        unsupported = artifact_content()
        unsupported["artifact_type"] = "arbitrary_blob"
        generated = artifact_content()
        generated["integrity"]["content_digest"] = "sha256:" + "0" * 64
        mismatched = json.loads(registration_request(artifact_content()))
        mismatched["content_digest"] = "sha256:" + "0" * 64

        cases = (
            (registration_request(unsupported), "not approved"),
            (registration_request(generated), "integrity"),
            (canonical_json_bytes(mismatched), "does not match"),
        )
        for request, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ArtifactRegistryContractError, message):
                    prepare_artifact_registration(request)

    def test_contract_rejects_duplicate_keys_floats_and_oversized_requests(self) -> None:
        duplicate = b'{"artifact_content":{},"artifact_content":{},"content_digest":"sha256:' + b"0" * 64 + b'"}'
        floating = b'{"artifact_content":{"value":1.5},"content_digest":"sha256:' + b"0" * 64 + b'"}'
        oversized = b"{" + b"x" * 1_100_000 + b"}"

        for request, message in (
            (duplicate, "duplicate JSON object key"),
            (floating, "floating-point"),
            (oversized, "bounded payload limit"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ArtifactRegistryContractError, message):
                    prepare_artifact_registration(request)

    def test_contract_rejects_excessive_json_nesting(self) -> None:
        depth = 2_000
        nested = (
            b'{"artifact_content":'
            + b"[" * depth
            + b"0"
            + b"]" * depth
            + b',"content_digest":"sha256:'
            + b"0" * 64
            + b'"}'
        )

        with self.assertRaisesRegex(ArtifactRegistryContractError, "nesting"):
            prepare_artifact_registration(nested)

    def test_each_approved_artifact_class_uses_its_contract_identity(self) -> None:
        work_start = artifact_content(artifact_id="work-start:delivery-810-v1")
        work_start["artifact_type"] = "delivery_art_work_start_record"
        review_packet = artifact_content()
        review_packet["artifact_type"] = "art_review_packet"
        review_packet["packet_id"] = "review-packet:delivery-810-v1"
        del review_packet["artifact_id"]

        for content, expected_identity in (
            (work_start, "work-start:delivery-810-v1"),
            (review_packet, "review-packet:delivery-810-v1"),
        ):
            with self.subTest(artifact_type=content["artifact_type"]):
                prepared = prepare_artifact_registration(registration_request(content))
                self.assertEqual(prepared.artifact_id, expected_identity)

    def test_register_rejects_an_invalid_storage_binding_before_metadata_commit(self) -> None:
        class InvalidBindingStore(FakeVersionedArtifactStore):
            def ensure_content(self, content_digest: str, body: bytes) -> StoredArtifactObject:
                return StoredArtifactObject(
                    object_key="delivery-art/sha256/not-the-content.json",
                    version_id="",
                    content_digest=content_digest,
                )

        registry = DeliveryArtifactRegistry(
            session_factory=self.sessions,
            storage=InvalidBindingStore(),
            service_identity_ref=SERVICE_IDENTITY_REF,
            implementation_ref=IMPLEMENTATION_REF,
            clock=lambda: FIXED_TIME,
        )

        with self.assertRaisesRegex(ArtifactStorageIntegrityError, "content-addressed"):
            registry.register(registration_request(artifact_content()), actor="oos")

        with self.sessions() as session:
            self.assertEqual(session.query(DeliveryArtifactCustodyReceipt).count(), 0)

    def test_database_failures_use_the_registry_unavailable_boundary(self) -> None:
        def unavailable_session_factory():
            raise SQLAlchemyError("database unavailable")

        registry = DeliveryArtifactRegistry(
            session_factory=unavailable_session_factory,
            storage=self.storage,
            service_identity_ref=SERVICE_IDENTITY_REF,
            implementation_ref=IMPLEMENTATION_REF,
            clock=lambda: FIXED_TIME,
        )

        with self.assertRaisesRegex(ArtifactRegistryUnavailable, "metadata store"):
            registry.read("sha256:" + "a" * 64, actor="oos")

    def test_read_detects_object_and_receipt_metadata_corruption(self) -> None:
        created = self.registry.register(registration_request(artifact_content()), actor="oos")
        digest = created.artifact["integrity"]["content_digest"]
        object_key = delivery_art_object_key(digest)
        version_id = self.storage.latest[object_key]
        original_body = self.storage.objects[object_key][version_id]
        self.storage.objects[object_key][version_id] = b"{}"
        with self.assertRaisesRegex(ArtifactStorageIntegrityError, "content digest"):
            self.registry.read(digest, actor="oos")

        self.storage.objects[object_key][version_id] = original_body
        receipt_id = created.custody_receipt["receipt_id"]
        with self.sessions.begin() as session:
            row = session.get(DeliveryArtifactCustodyReceipt, receipt_id)
            assert row is not None
            row.receipt_uri = "wgcf://receipts/artifact-custody/" + "0" * 24 + "-" + "0" * 64 + ".json"
        with self.assertRaisesRegex(ArtifactStorageIntegrityError, "reference|URI"):
            self.registry.read(digest, actor="oos")

    def test_receipt_payload_tampering_fails_reconciliation(self) -> None:
        created = self.registry.register(registration_request(artifact_content()), actor="oos")
        digest = created.artifact["integrity"]["content_digest"]
        receipt_id = created.custody_receipt["receipt_id"]
        with self.sessions.begin() as session:
            row = session.get(DeliveryArtifactCustodyReceipt, receipt_id)
            assert row is not None
            tampered = copy.deepcopy(row.receipt)
            tampered["issuer"]["owner_repo"] = "another-owner"
            row.receipt = tampered

        with self.assertRaisesRegex(ArtifactStorageIntegrityError, "digest"):
            self.registry.reconcile(digest, actor="wgcf")

    def test_runtime_requires_an_exact_implementation_commit(self) -> None:
        with self.assertRaisesRegex(ArtifactRegistryUnavailable, "exact Git commit"):
            DeliveryArtifactRegistry(
                session_factory=self.sessions,
                storage=self.storage,
                service_identity_ref=SERVICE_IDENTITY_REF,
                implementation_ref="main",
            )


class ArtifactRegistryAuthorizerTests(TestCase):
    def setUp(self) -> None:
        self.authorizer = ArtifactRegistryAuthorizer(
            oos_secret="o" * 32,
            reconciler_secret="r" * 32,
        )

    def test_callers_have_separate_method_scopes(self) -> None:
        self.authorizer.authorize("operator-orchestration-service", "o" * 32, "register")
        self.authorizer.authorize("operator-orchestration-service", "o" * 32, "read")
        self.authorizer.authorize("workspace-governance-control-fabric", "r" * 32, "reconcile")

        with self.assertRaises(ArtifactRegistryForbidden):
            self.authorizer.authorize("operator-orchestration-service", "o" * 32, "reconcile")
        with self.assertRaises(ArtifactRegistryForbidden):
            self.authorizer.authorize("workspace-governance-control-fabric", "r" * 32, "register")
        with self.assertRaises(ArtifactRegistryUnauthorized):
            self.authorizer.authorize("operator-orchestration-service", "wrong", "read")


class ArtifactStorageSettingsTests(TestCase):
    def test_storage_endpoint_rejects_embedded_credentials(self) -> None:
        with self.assertRaisesRegex(ArtifactStorageUnavailable, "embed credentials"):
            S3ArtifactObjectStore(
                ArtifactStorageSettings(
                    endpoint="http://access:secret@object-store.test",
                    bucket="delivery-art",
                    access_key="access",
                    secret_key="secret",
                ),
            )
