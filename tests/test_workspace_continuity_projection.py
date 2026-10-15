from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/control_fabric_core/src"))

from control_fabric_core.db.models import (  # noqa: E402
    ProductPublicationReadinessRecord,
    metadata,
)
from control_fabric_core.workspace_continuity_projection import (  # noqa: E402
    WorkspaceContinuityProjectionService,
    WorkspaceContinuityProjectionUnavailable,
)
from control_fabric_core.workspace_inventory_contracts import (  # noqa: E402
    InventorySnapshot,
    digest,
)


NOW = datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc)
REVISION = "a" * 40


class StubAuthority:
    def __init__(self, snapshot: InventorySnapshot) -> None:
        self.value = snapshot

    def snapshot(self) -> InventorySnapshot:
        return copy.deepcopy(self.value)


class WorkspaceContinuityProjectionTests(TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite://")
        metadata.create_all(engine)
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self.snapshot = _snapshot()
        self.service = WorkspaceContinuityProjectionService(
            session_factory=self.sessions,
            authority=StubAuthority(self.snapshot),
            clock=lambda: NOW,
        )

    def test_case_1294_positive_projects_one_canonical_identity_across_sources(self) -> None:
        self._store_readiness(expires_at=NOW + timedelta(minutes=5))

        result = self.service.read()

        self.assertEqual(result["projection_state"], "current")
        self.assertEqual(result["authority"]["revision"], REVISION)
        product = result["products"][0]
        self.assertEqual(product["product_id"], "product:console")
        self.assertEqual(product["latest_lifecycle_event"]["action"], "restore")
        self.assertEqual(product["publication_readiness"]["control_state"], "current")
        self.assertTrue(product["publication_readiness"]["actionable"])
        self.assertEqual(result["intake"][0]["record_id"], "component:temporal")
        self.assertEqual(result["authority_boundary"]["mutation_authority"], "none")

    def test_case_1294_negative_stale_readiness_is_visible_but_not_actionable(self) -> None:
        self._store_readiness(
            expires_at=NOW - timedelta(seconds=1),
            authority_revision="b" * 40,
        )

        publication = self.service.read()["products"][0]["publication_readiness"]

        self.assertEqual(publication["control_state"], "stale")
        self.assertFalse(publication["actionable"])
        self.assertEqual(
            publication["stale_reasons"],
            ["expired", "authority-revision-changed"],
        )

    def test_case_1294_negative_non_workflow_caller_readiness_is_not_authoritative(self) -> None:
        self._store_readiness(
            expires_at=NOW + timedelta(minutes=5),
            actor="untrusted-reader",
        )

        publication = self.service.read()["products"][0]["publication_readiness"]

        self.assertEqual(publication["control_state"], "unavailable")
        self.assertFalse(publication["actionable"])

    def test_case_1294_negative_invalid_lineage_fails_closed(self) -> None:
        product = self.snapshot.records["products"]["products"]["console"]
        product["record"]["lineage"]["source"] = "workspace-intake"
        product["record"]["lineage"]["intake_entry_version"] = None

        with self.assertRaises(WorkspaceContinuityProjectionUnavailable):
            self.service.read()

    def test_case_1294_negative_active_and_intake_identity_collision_fails_closed(self) -> None:
        candidate = copy.deepcopy(
            self.snapshot.records["intake-register"]["components"]["temporal"],
        )
        candidate["record"]["id"] = "product:console"
        self.snapshot.records["intake-register"]["products"]["console"] = candidate

        with self.assertRaises(WorkspaceContinuityProjectionUnavailable):
            self.service.read()

    def test_case_1294_negative_history_must_match_current_registry_state(self) -> None:
        self.snapshot.records["workspace-inventory-history"]["events"][0]["after"][
            "record_version"
        ] = 99

        with self.assertRaises(WorkspaceContinuityProjectionUnavailable):
            self.service.read()

    def test_packaged_runtime_uses_explicit_continuity_schema_path(self) -> None:
        source = REPO_ROOT / "schemas/workspace-continuity-projection.schema.json"
        with TemporaryDirectory() as directory:
            packaged = Path(directory) / "workspace-continuity-projection.schema.json"
            packaged.write_bytes(source.read_bytes())
            with patch.dict(
                os.environ,
                {"WGCF_WORKSPACE_CONTINUITY_SCHEMA_PATH": str(packaged)},
            ):
                service = WorkspaceContinuityProjectionService(
                    session_factory=self.sessions,
                    authority=StubAuthority(self.snapshot),
                    clock=lambda: NOW,
                )

        self.assertEqual(service.read()["projection_state"], "current")

    def test_packaged_runtime_fails_closed_when_bound_schema_is_missing(self) -> None:
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.schema.json"
            with patch.dict(
                os.environ,
                {"WGCF_WORKSPACE_CONTINUITY_SCHEMA_PATH": str(missing)},
            ):
                with self.assertRaises(WorkspaceContinuityProjectionUnavailable):
                    WorkspaceContinuityProjectionService(
                        session_factory=self.sessions,
                        authority=StubAuthority(self.snapshot),
                        clock=lambda: NOW,
                    )

    def _store_readiness(
        self,
        *,
        expires_at: datetime,
        authority_revision: str = REVISION,
        actor: str = "operator-orchestration-service",
    ) -> None:
        readiness = {
            "schema_version": 2,
            "artifact_type": "product-publication-readiness",
            "readiness_id": "product-publication-readiness:test",
            "product_id": "product:console",
            "outcome": "ready",
            "action": "restore",
            "current_publication_posture": "unlisted",
            "target_publication": {
                "posture": "internal",
                "listing_scope": "workspace",
                "audience": "internal",
                "access_class": "authenticated",
            },
            "authority_binding": {"revision": authority_revision},
        }
        readiness["readiness_digest"] = digest(readiness)
        row = ProductPublicationReadinessRecord(
            evaluation_id="product-publication-evaluation:test",
            evaluation_digest="sha256:" + "1" * 64,
            readiness_digest=readiness["readiness_digest"],
            actor=actor,
            request_id="product-publication-request:test",
            idempotency_key="product-publication:test",
            request_digest="sha256:" + "2" * 64,
            authority_revision=authority_revision,
            contract_digest="sha256:" + "3" * 64,
            implementation_ref="4" * 40,
            expires_at=expires_at,
            readiness=readiness,
        )
        with self.sessions.begin() as session:
            session.add(row)


def _snapshot() -> InventorySnapshot:
    lineage = {
        "source": "legacy-migration",
        "source_ref": "git://workspace-governance/source",
        "source_digest": "sha256:" + "5" * 64,
        "intake_entry_version": None,
    }
    last_mutation = {
        "id": "workspace-inventory-lifecycle:restore",
        "action": "restore",
        "idempotency_key": "restore:console",
        "request_ref": "workspace-inventory-lifecycle-request:restore",
        "request_digest": "sha256:" + "6" * 64,
        "readiness_ref": "workspace-inventory-lifecycle-readiness:restore",
        "readiness_digest": "sha256:" + "7" * 64,
        "applied_at": "2026-10-14T20:00:00Z",
    }
    product = {
        "record": {
            "id": "product:console",
            "version": 2,
            "lineage": lineage,
            "last_mutation": last_mutation,
        },
        "posture": "active",
        "maturity": "platform-integrated",
        "lifecycle": "platform-integrated",
        "platform_owner": "platform-engineering",
        "security_owner": "security-architecture",
        "runtime_owner": "governance-operations-console",
        "source_owners": ["governance-operations-console"],
        "stage_supported": False,
        "governed_prod_promotion": False,
        "highest_real_endpoint": "dev-integration",
        "validation_behavior": {
            "posture": "covered",
            "wgcf_graph_role": "product-readiness-aggregate",
            "catalog_refs": [],
            "notes": "test",
        },
    }
    event = {
        "event_id": "workspace-inventory-event:product:console:2",
        "sequence": 1,
        "target": {"kind": "product", "name": "console", "record_id": "product:console"},
        "action": "restore",
        "after": {
            "record_version": 2,
            "record_digest": digest(product),
            "posture": "active",
        },
        "applied_at": "2026-10-14T20:00:00Z",
    }
    event["event_digest"] = digest(event)
    intake = {
        "status": "proposed",
        "decision_source": "operator",
        "owner_route": "platform-engineering",
        "record": {
            "id": "component:temporal",
            "version": 1,
            "source": {
                "class": "direct",
                "ref": "git://workspace-governance/intake",
                "digest": "sha256:" + "8" * 64,
            },
            "decision": {
                "id": "decision:temporal",
                "ref": "git://workspace-governance/intake",
                "digest": "sha256:" + "9" * 64,
                "source": "operator",
                "operator_ref": "operator:test",
                "decided_at": "2026-10-14T19:00:00Z",
            },
            "last_mutation": {
                "id": "intake:temporal",
                "idempotency_key": "intake:temporal",
                "request_ref": "request:temporal",
                "request_digest": "sha256:" + "a" * 64,
                "decision_ref": "decision:temporal",
                "decision_digest": "sha256:" + "9" * 64,
                "applied_at": "2026-10-14T19:00:00Z",
            },
        },
    }
    return InventorySnapshot(
        revision=REVISION,
        records={
            "intake-register": {
                "schema_version": 2,
                "repos": {},
                "products": {},
                "components": {"temporal": intake},
            },
            "repos": {"schema_version": 2, "repos": {}, "retired_repos": {}},
            "products": {"schema_version": 2, "products": {"console": product}},
            "components": {"schema_version": 2, "components": {}},
            "workspace-inventory-history": {"schema_version": 1, "events": [event]},
        },
        file_digests={"contracts/products.yaml": "sha256:" + "b" * 64},
    )
