from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile
from unittest import TestCase

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core.artifact_registry import DeliveryArtifactRegistry
from control_fabric_core.artifact_storage import StoredArtifactObject, delivery_art_object_key
from control_fabric_core.canonical_json import (
    canonical_digest,
    canonical_json_bytes,
    delivery_art_content_projection,
)
from control_fabric_core.db import metadata
from control_fabric_core.db.models import DeliveryArtReadinessReceipt, LedgerEvent
from control_fabric_core.delivery_art_contracts import (
    DeliveryArtContractBundle,
    DeliveryArtContractError,
    operating_readiness_subject,
)
from control_fabric_core.delivery_art_readiness import (
    DeliveryArtReadinessContractError,
    DeliveryArtReadinessService,
)


FIXTURE_ROOT = REPO_ROOT / "contracts" / "delivery-art" / "fixtures"
IMPLEMENTATION_REF = "f" * 40
SERVICE_IDENTITY_REF = (
    "kubernetes://devint-governance-control-fabric/"
    "serviceaccount/workspace-governance-control-fabric-api"
)


class FakeVersionedStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[str, bytes]] = {}

    def ensure_content(self, content_digest: str, body: bytes) -> StoredArtifactObject:
        key = delivery_art_object_key(content_digest)
        current = self.objects.get(key)
        if current is None:
            current = (f"version-{len(self.objects) + 1}", body)
            self.objects[key] = current
        elif current[1] != body:
            raise AssertionError("content-addressed fake store collision")
        return StoredArtifactObject(key, current[0], content_digest)

    def read_version(self, object_key: str, version_id: str) -> bytes:
        stored_version, body = self.objects[object_key]
        if stored_version != version_id:
            raise KeyError(version_id)
        return body


class SequenceClock:
    def __init__(self) -> None:
        self.values = iter(
            [
                datetime(2026, 8, 8, 2, 5, tzinfo=timezone.utc),
                datetime(2026, 8, 8, 2, 10, tzinfo=timezone.utc),
                datetime(2026, 8, 8, 3, 15, tzinfo=timezone.utc),
                datetime(2026, 8, 8, 3, 45, tzinfo=timezone.utc),
            ],
        )

    def __call__(self) -> datetime:
        return next(self.values)


def fixture(name: str) -> dict:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def registration_request(artifact: dict) -> bytes:
    content = delivery_art_content_projection(artifact)
    return canonical_json_bytes(
        {
            "artifact_content": content,
            "content_digest": canonical_digest(content),
        },
    )


def source_ref(artifact: dict) -> dict[str, str]:
    return {
        "uri": artifact["custody"]["uri"],
        "digest": artifact["integrity"]["content_digest"],
    }


def readiness_request(
    artifact: dict,
    level: str,
    *,
    digest_kind: str = "artifact-content",
    digest: str | None = None,
    candidate: dict | None = None,
) -> bytes:
    identifier = artifact.get("artifact_id") or artifact.get("packet_id")
    payload = {
        "schema_version": 1,
        "profile_id": "dev-integration",
        "readiness_request": {
            "artifact_id": identifier,
            "artifact_type": artifact["artifact_type"],
            "covered_work_item_ids": artifact["covered_work_item_ids"],
            "delivery_id": artifact["delivery_id"],
            "digest": digest or artifact["integrity"]["content_digest"],
            "digest_kind": digest_kind,
            "readiness_level": level,
        },
    }
    if candidate is None:
        payload["subject_ref"] = source_ref(artifact)
    else:
        payload["finalization_candidate"] = candidate
    return canonical_json_bytes(payload)


def local_finalization_candidate(merge_ready: dict) -> dict:
    candidate = fixture("review-packet-finalized.valid.json")
    candidate["status"] = "draft"
    candidate["finalized_at"] = None
    candidate["work_start"] = copy.deepcopy(merge_ready["work_start"])
    candidate["readiness"] = {
        "evaluated_at": merge_ready["readiness"]["evaluated_at"],
        "level": "implementation-ready",
        "receipt_refs": [],
        "subject_digest": None,
    }
    candidate["custody"] = {
        "backend": "local-filesystem",
        "persisted_at": None,
        "receipt_ref": None,
        "state": "local-draft",
        "supersedes": source_ref(merge_ready),
        "uri": "local://delivery-art/review-packet-finalization.json",
    }
    candidate["integrity"]["content_digest"] = canonical_digest(
        delivery_art_content_projection(candidate),
    )
    return candidate


def validation_only_review_packet() -> dict:
    packet = fixture("review-packet-merge-ready.valid.json")
    test_evidence_ids = {entry["id"] for entry in packet["evidence"]["tests"]}
    packet["evidence"]["tests"] = []
    for mapping in packet["evidence"]["acceptance_mapping"]:
        mapping["evidence_ids"] = [
            evidence_id
            for evidence_id in mapping["evidence_ids"]
            if evidence_id not in test_evidence_ids
        ]
    packet["integrity"]["content_digest"] = canonical_digest(
        delivery_art_content_projection(packet),
    )
    packet["custody"]["uri"] = (
        "wgcf://artifacts/delivery-art/sha256/"
        + packet["integrity"]["content_digest"].removeprefix("sha256:")
    )
    return packet


class DeliveryArtReadinessTests(TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.registry = DeliveryArtifactRegistry(
            session_factory=self.sessions,
            storage=FakeVersionedStore(),
            service_identity_ref=SERVICE_IDENTITY_REF,
            implementation_ref=IMPLEMENTATION_REF,
            clock=SequenceClock(),
        )
        self.contract_bundle = DeliveryArtContractBundle.load(
            REPO_ROOT / "contracts/delivery-art",
        )
        self.service = DeliveryArtReadinessService(
            session_factory=self.sessions,
            artifact_registry=self.registry,
            service_identity_ref=SERVICE_IDENTITY_REF,
            implementation_ref=IMPLEMENTATION_REF,
            contract_bundle=self.contract_bundle,
            clock=lambda: datetime(2026, 8, 8, 3, 30, tzinfo=timezone.utc),
        )
        self.architecture = self._register(fixture("architecture-packet.valid.json"))
        self.work_start = self._register(fixture("work-start-record.valid.json"))
        self.merge_ready = self._register(fixture("review-packet-merge-ready.valid.json"))

    def tearDown(self) -> None:
        self.engine.dispose()

    def _register(self, artifact: dict) -> dict:
        return self.registry.register(
            registration_request(artifact),
            actor="operator-orchestration-service",
        ).artifact

    def _issue_operating_candidate(self, candidate: dict) -> dict:
        candidate["integrity"]["content_digest"] = canonical_digest(
            delivery_art_content_projection(candidate),
        )
        subject = operating_readiness_subject(candidate)
        return self.service.issue(
            readiness_request(
                candidate,
                "operating-ready",
                digest_kind="readiness-subject",
                digest=subject["readiness"]["subject_digest"],
                candidate=candidate,
            ),
            actor="operator-orchestration-service",
        ).artifact

    def test_validation_only_review_packet_accepts_empty_test_evidence(self) -> None:
        packet = validation_only_review_packet()

        self.assertEqual(self.contract_bundle.validation_errors(packet), ())

    def test_source_backed_review_packet_still_requires_validation_evidence(self) -> None:
        packet = validation_only_review_packet()
        packet["evidence"]["validations"] = []
        packet["integrity"]["content_digest"] = canonical_digest(
            delivery_art_content_projection(packet),
        )
        packet["custody"]["uri"] = (
            "wgcf://artifacts/delivery-art/sha256/"
            + packet["integrity"]["content_digest"].removeprefix("sha256:")
        )

        self.assertTrue(
            any(
                error.startswith("$.evidence.validations:")
                for error in self.contract_bundle.validation_errors(packet)
            ),
        )

    def test_four_readiness_levels_issue_content_addressed_receipts(self) -> None:
        architecture = self.service.issue(
            readiness_request(self.architecture, "architecture-ready"),
            actor="operator-orchestration-service",
        )
        implementation = self.service.issue(
            readiness_request(self.work_start, "implementation-ready"),
            actor="operator-orchestration-service",
        )
        merge = self.service.issue(
            readiness_request(self.merge_ready, "merge-ready"),
            actor="operator-orchestration-service",
        )

        candidate = local_finalization_candidate(self.merge_ready)
        subject = operating_readiness_subject(candidate)
        operating = self.service.issue(
            readiness_request(
                candidate,
                "operating-ready",
                digest_kind="readiness-subject",
                digest=subject["readiness"]["subject_digest"],
                candidate=candidate,
            ),
            actor="operator-orchestration-service",
        )

        for result, level in (
            (architecture, "architecture-ready"),
            (implementation, "implementation-ready"),
            (merge, "merge-ready"),
            (operating, "operating-ready"),
        ):
            receipt = result.artifact
            self.assertEqual(receipt["readiness"]["level"], level)
            self.assertEqual(receipt["readiness"]["outcome"], "ready")
            self.assertTrue(receipt["readiness"]["mutation_allowed"])
            self.assertIn(
                receipt["integrity"]["content_digest"].removeprefix("sha256:"),
                receipt["custody"]["uri"],
            )
        self.assertEqual(operating.artifact["subject"]["digest_kind"], "readiness-subject")
        self.assertEqual(operating.artifact["subject"]["digest"], subject["readiness"]["subject_digest"])

    def test_identical_evaluation_reuses_receipt_and_read_preserves_binding(self) -> None:
        raw = readiness_request(self.architecture, "architecture-ready")
        created = self.service.issue(raw, actor="operator-orchestration-service")
        replay = self.service.issue(raw, actor="operator-orchestration-service")
        token = created.artifact["receipt_id"].split(":", 1)[1]
        read = self.service.read(token, actor="operator-orchestration-service")

        self.assertEqual(created.resolution, "created")
        self.assertEqual(replay.resolution, "reused")
        self.assertEqual(read.resolution, "read")
        self.assertEqual(created.artifact, replay.artifact)
        self.assertEqual(created.artifact, read.artifact)
        with self.sessions() as session:
            self.assertEqual(len(session.scalars(select(DeliveryArtReadinessReceipt)).all()), 1)
            actions = {event.action for event in session.scalars(select(LedgerEvent)).all()}
        self.assertIn("delivery-art.readiness.persisted", actions)
        self.assertIn("delivery-art.readiness.reused", actions)
        self.assertIn("delivery-art.readiness.read", actions)

    def test_blocked_architecture_issues_non_mutating_receipt(self) -> None:
        engine = create_engine("sqlite:///:memory:")
        metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        registry = DeliveryArtifactRegistry(
            session_factory=sessions,
            storage=FakeVersionedStore(),
            service_identity_ref=SERVICE_IDENTITY_REF,
            implementation_ref=IMPLEMENTATION_REF,
            clock=lambda: datetime(2026, 8, 8, 2, 5, tzinfo=timezone.utc),
        )
        blocked = fixture("architecture-packet.valid.json")
        blocked["decision"]["status"] = "blocked-pending-architecture-decision"
        blocked["architecture"]["contradictions_open_decisions"][0]["status"] = "open"
        blocked["architecture"]["contradictions_open_decisions"][0]["resolution"] = None
        blocked["scope_fingerprint"] = canonical_digest(
            {
                "schema_version": blocked["schema_version"],
                "artifact_type": blocked["artifact_type"],
                "delivery_id": blocked["delivery_id"],
                "covered_work_item_ids": blocked["covered_work_item_ids"],
                "source_snapshot": blocked["source_snapshot"],
                "architecture": blocked["architecture"],
                "conformance_plan": blocked["conformance_plan"],
                "decision_status": blocked["decision"]["status"],
            },
        )
        blocked["integrity"]["content_digest"] = canonical_digest(
            delivery_art_content_projection(blocked),
        )
        durable = registry.register(
            registration_request(blocked),
            actor="operator-orchestration-service",
        ).artifact
        service = DeliveryArtReadinessService(
            session_factory=sessions,
            artifact_registry=registry,
            service_identity_ref=SERVICE_IDENTITY_REF,
            implementation_ref=IMPLEMENTATION_REF,
            clock=lambda: datetime(2026, 8, 8, 3, 30, tzinfo=timezone.utc),
        )

        result = service.issue(
            readiness_request(durable, "architecture-ready"),
            actor="operator-orchestration-service",
        )

        self.assertEqual(result.artifact["readiness"]["outcome"], "blocked")
        self.assertFalse(result.artifact["readiness"]["mutation_allowed"])
        self.assertEqual(
            {finding["id"] for finding in result.artifact["findings"]},
            {"architecture-decision-blocked", "architecture-decisions-open"},
        )
        engine.dispose()

    def test_changed_subject_appends_superseding_receipt_generation(self) -> None:
        first = self.service.issue(
            readiness_request(self.architecture, "architecture-ready"),
            actor="operator-orchestration-service",
        )
        corrected = copy.deepcopy(self.architecture)
        corrected["decision"]["status"] = "blocked-pending-architecture-decision"
        corrected["architecture"]["contradictions_open_decisions"][0]["status"] = "open"
        corrected["architecture"]["contradictions_open_decisions"][0]["resolution"] = None
        corrected["custody"]["supersedes"] = source_ref(self.architecture)
        corrected["scope_fingerprint"] = canonical_digest(
            {
                "schema_version": corrected["schema_version"],
                "artifact_type": corrected["artifact_type"],
                "delivery_id": corrected["delivery_id"],
                "covered_work_item_ids": corrected["covered_work_item_ids"],
                "source_snapshot": corrected["source_snapshot"],
                "architecture": corrected["architecture"],
                "conformance_plan": corrected["conformance_plan"],
                "decision_status": corrected["decision"]["status"],
            },
        )
        corrected["integrity"]["content_digest"] = canonical_digest(
            delivery_art_content_projection(corrected),
        )
        durable = self._register(corrected)

        second = self.service.issue(
            readiness_request(durable, "architecture-ready"),
            actor="operator-orchestration-service",
        )

        self.assertEqual(first.generation, 1)
        self.assertEqual(second.generation, 2)
        self.assertEqual(second.artifact["readiness"]["outcome"], "blocked")
        self.assertEqual(
            second.artifact["custody"]["supersedes"],
            {
                "uri": first.artifact["custody"]["uri"],
                "digest": first.artifact["integrity"]["content_digest"],
            },
        )

    def test_operating_readiness_allows_monotonic_post_merge_acceptance_evidence(self) -> None:
        candidate = local_finalization_candidate(self.merge_ready)
        live_evidence = copy.deepcopy(candidate["evidence"]["tests"][0])
        live_evidence.update(
            {
                "id": "evidence:post-merge-runtime",
                "name": "Post-merge runtime reconciliation",
                "summary": "The merged source passed its required live reconciliation.",
            },
        )
        candidate["evidence"]["runtime_and_live"].append(live_evidence)
        candidate["evidence"]["acceptance_mapping"][0]["evidence_ids"].append(
            live_evidence["id"],
        )

        result = self._issue_operating_candidate(candidate)

        self.assertEqual(result["readiness"]["outcome"], "ready")
        self.assertNotIn(
            "merge-ready-evidence-rewritten",
            {finding["id"] for finding in result["findings"]},
        )

    def test_operating_readiness_blocks_removed_or_replaced_acceptance_evidence(self) -> None:
        for replacement in (None, "evidence:post-merge-runtime"):
            with self.subTest(replacement=replacement):
                candidate = local_finalization_candidate(self.merge_ready)
                evidence_ids = candidate["evidence"]["acceptance_mapping"][0]["evidence_ids"]
                evidence_ids.remove("evidence:schema-negative-cases")
                if replacement:
                    replacement_evidence = copy.deepcopy(candidate["evidence"]["tests"][0])
                    replacement_evidence["id"] = replacement
                    candidate["evidence"]["runtime_and_live"].append(replacement_evidence)
                    evidence_ids.append(replacement)

                result = self._issue_operating_candidate(candidate)

                self.assertEqual(result["readiness"]["outcome"], "blocked")
                self.assertIn(
                    "merge-ready-evidence-rewritten",
                    {finding["id"] for finding in result["findings"]},
                )

    def test_operating_readiness_blocks_changed_acceptance_meaning(self) -> None:
        for field in ("acceptance_ref", "summary"):
            with self.subTest(field=field):
                candidate = local_finalization_candidate(self.merge_ready)
                candidate["evidence"]["acceptance_mapping"][0][field] = "Changed after review."

                result = self._issue_operating_candidate(candidate)

                self.assertEqual(result["readiness"]["outcome"], "blocked")
                self.assertIn(
                    "merge-ready-evidence-rewritten",
                    {finding["id"] for finding in result["findings"]},
                )

    def test_operating_readiness_blocks_rewritten_merge_ready_evidence(self) -> None:
        for section in ("tests", "validations"):
            with self.subTest(section=section):
                candidate = local_finalization_candidate(self.merge_ready)
                candidate["evidence"][section][0]["summary"] = "Rewritten after review."

                result = self._issue_operating_candidate(candidate)

                self.assertEqual(result["readiness"]["outcome"], "blocked")
                self.assertIn(
                    "merge-ready-evidence-rewritten",
                    {finding["id"] for finding in result["findings"]},
                )

    def test_request_rejects_subject_digest_substitution(self) -> None:
        raw = json.loads(readiness_request(self.architecture, "architecture-ready"))
        raw["readiness_request"]["digest"] = f"sha256:{'0' * 64}"
        with self.assertRaisesRegex(
            DeliveryArtReadinessContractError,
            "subject_ref digest does not match",
        ):
            self.service.issue(
                canonical_json_bytes(raw),
                actor="operator-orchestration-service",
            )

    def test_contract_bundle_rejects_schema_bytes_outside_pinned_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            contract_root = Path(temp_dir) / "delivery-art"
            shutil.copytree(REPO_ROOT / "contracts/delivery-art", contract_root)
            schema_path = contract_root / "delivery-art-readiness-receipt.schema.json"
            schema_path.write_bytes(schema_path.read_bytes() + b"\n")

            with self.assertRaisesRegex(
                DeliveryArtContractError,
                "does not match manifest",
            ):
                DeliveryArtContractBundle.load(contract_root)
