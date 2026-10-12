from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
import yaml

from control_fabric_core.artifact_registry import ArtifactRegistryAuthorizer
from control_fabric_core.db.models import (
    LedgerEvent,
    ProductPublicationReadinessRecord,
    metadata,
)
from control_fabric_core.product_publication_contracts import (
    ProductPublicationAuthority,
    ProductPublicationContracts,
    ProductPublicationUnavailable,
    artifact_digest,
    canonical_bytes,
    digest,
)
from control_fabric_core.product_publication_readiness import (
    ProductPublicationReadinessService,
    build_product_publication_readiness_runtime,
)
from wgcf_api.app import create_app


class ProductPublicationReadinessTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace-governance"
        self.owner = self.root / "governance-operations-console"
        self._init_repo(self.workspace)
        self._init_repo(self.owner)

        policy = self.policy()
        self.policy_raw = yaml.safe_dump(policy, sort_keys=False).encode()
        self.schema_raw = json.dumps(self.manifest_schema(), sort_keys=True).encode()
        self.history_schema_raw = json.dumps(self.history_schema(), sort_keys=True).encode()
        self._write(self.workspace, "contracts/product-publication.yaml", self.policy_raw)
        self._write(
            self.workspace,
            "contracts/schemas/product-publication-manifest.schema.json",
            self.schema_raw,
        )
        self._write(
            self.workspace,
            "contracts/schemas/workspace-inventory-history.schema.json",
            self.history_schema_raw,
        )
        self.products = {
            "schema_version": 2,
            "products": {
                "governance-operations-console": {
                    "record": {"version": 1},
                    "maturity": "owner-managed",
                    "posture": "active",
                    "runtime_owner": "governance-operations-console",
                    "source_owners": ["governance-operations-console"],
                }
            },
        }
        self._write(
            self.workspace,
            "contracts/products.yaml",
            yaml.safe_dump(self.products, sort_keys=False).encode(),
        )
        self.history = {"schema_version": 1, "events": []}
        self._write(
            self.workspace,
            "contracts/workspace-inventory-history.yaml",
            yaml.safe_dump(self.history, sort_keys=False).encode(),
        )
        self._commit(self.workspace, "Authority contract")
        self.authority_revision = self._git(self.workspace, "rev-parse", "HEAD")

        self._write(self.owner, "src/app.txt", b"operational product\n")
        self._commit(self.owner, "Product source")
        self.source_revision = self._git(self.owner, "rev-parse", "HEAD")
        self.manifest = self.owner_manifest()
        self._write(
            self.owner,
            "product-publication/manifest.json",
            json.dumps(self.manifest, indent=2).encode(),
        )
        self._commit(self.owner, "Add publication manifest")
        self.manifest_revision = self._git(self.owner, "rev-parse", "HEAD")

        contracts = ProductPublicationContracts.load()
        self.contracts = replace(
            contracts,
            manifest={
                **contracts.manifest,
                "authority_commit": self.authority_revision,
                "authority_files": {
                    "contracts/product-publication.yaml": hashlib.sha256(self.policy_raw).hexdigest(),
                    "contracts/schemas/product-publication-manifest.schema.json": hashlib.sha256(self.schema_raw).hexdigest(),
                    "contracts/schemas/workspace-inventory-history.schema.json": hashlib.sha256(self.history_schema_raw).hexdigest(),
                },
            },
        )
        self.authority = ProductPublicationAuthority(
            self.workspace,
            {"governance-operations-console": self.owner},
            self.contracts,
            trusted_ref="refs/heads/main",
        )
        self.engine = create_engine(f"sqlite:///{self.root / 'ledger.sqlite'}")
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.service = ProductPublicationReadinessService(
            session_factory=self.sessions,
            authority=self.authority,
            service_identity_ref="spiffe://test/product-publication",
            implementation_ref="1" * 40,
            clock=lambda: datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc),
        )
        self.client = TestClient(
            create_app(
                product_publication_readiness=self.service,
                artifact_registry_authorizer=ArtifactRegistryAuthorizer(
                    oos_secret="o" * 32,
                    reconciler_secret="r" * 32,
                ),
            )
        )
        self.addCleanup(self.client.close)
        self.headers = {
            "x-wgcf-caller-id": "operator-orchestration-service",
            "x-wgcf-caller-secret": "o" * 32,
        }

    @staticmethod
    def policy() -> dict:
        return {
            "schema_version": 1,
            "product_publication": {
                "action_bindings": {
                    "publish": {"allowed_source_postures": ["unlisted"], "allowed_target_postures": ["internal", "client", "public"]},
                    "update": {"allowed_source_postures": ["internal", "client", "public"], "allowed_target_postures": ["internal", "client", "public"]},
                    "unpublish": {"allowed_source_postures": ["internal", "client", "public"], "allowed_target_postures": ["unlisted"]},
                    "archive": {"allowed_source_postures": ["unlisted"], "allowed_target_postures": ["archived"]},
                    "restore": {"allowed_source_postures": ["archived"], "allowed_target_postures": ["unlisted"]},
                },
                "posture_bindings": {
                    "unlisted": {"listing_scope": None, "audience": None, "access_class": None, "allowed_lifecycle_states": ["active", "suspended", "retired"]},
                    "internal": {"listing_scope": "workspace-portfolio", "audience": "workspace-operators", "access_class": "workspace-authenticated", "allowed_lifecycle_states": ["active"]},
                    "client": {"listing_scope": "client-portfolio", "audience": "authorized-clients", "access_class": "client-authorized", "allowed_lifecycle_states": ["active"]},
                    "public": {"listing_scope": "public-catalog", "audience": "public", "access_class": "public-anonymous", "allowed_lifecycle_states": ["active"]},
                    "archived": {"listing_scope": "historical-archive", "audience": "historical", "access_class": "historical-read-only", "allowed_lifecycle_states": ["retired"]},
                },
            },
        }

    @staticmethod
    def manifest_schema() -> dict:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": [
                "schema_version", "artifact_type", "manifest_id", "product_id",
                "product_record_ref", "product_record_version", "product_classification",
                "lifecycle_state", "owner_repo", "owner_role", "owner_revision",
                "source_refs", "lifecycle_evidence", "issued_at",
            ],
            "properties": {
                "schema_version": {"enum": [1, 2]},
                "inventory_lifecycle_binding": {"type": "object"},
            },
            "allOf": [
                {
                    "if": {
                        "properties": {"schema_version": {"const": 2}},
                        "required": ["schema_version"],
                    },
                    "then": {"required": ["inventory_lifecycle_binding"]},
                }
            ],
            "additionalProperties": True,
        }

    @staticmethod
    def history_schema() -> dict:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["schema_version", "events"],
            "properties": {
                "schema_version": {"const": 1},
                "events": {"type": "array"},
            },
            "additionalProperties": False,
        }

    def owner_manifest(self) -> dict:
        return {
            "schema_version": 1,
            "artifact_type": "product-publication-owner-manifest",
            "manifest_id": "product-publication-manifest:governance-operations-console:1",
            "product_id": "product:governance-operations-console",
            "product_record_ref": "workspace-governance://products/governance-operations-console/v1",
            "product_record_version": 1,
            "product_classification": "owner-managed",
            "lifecycle_state": "active",
            "owner_repo": "governance-operations-console",
            "owner_role": "runtime-owner",
            "owner_revision": self.source_revision,
            "source_refs": [{"repo": "governance-operations-console", "revision": self.source_revision, "path": "src/app.txt"}],
            "lifecycle_evidence": {
                "project_phase": "operational",
                "source_custody": "dedicated-owner-repo",
                "runtime_environment": "local-preview",
                "release_posture": "not-applicable",
                "publication_posture": "unlisted",
                "evidence_refs": [f"git://governance-operations-console/{self.source_revision}"],
            },
            "issued_at": "2026-10-12T04:30:00Z",
        }

    def install_lifecycle_state(self, action: str) -> None:
        if action not in {"archive", "restore"}:
            raise ValueError(action)
        product = copy.deepcopy(
            self.products["products"]["governance-operations-console"]
        )
        before = {
            "record_version": product["record"]["version"],
            "record_digest": digest(product),
            "posture": product["posture"],
        }
        events = []
        previous_ref = None

        product["posture"] = "retired"
        product["record"]["version"] = 2
        product["record"]["last_mutation"] = {
            "id": "workspace-inventory-lifecycle:retire-console",
            "action": "retire",
            "idempotency_key": "retire-console",
            "request_ref": "workspace-inventory-lifecycle-request:retire-console",
            "request_digest": digest({"request": "retire-console"}),
            "readiness_ref": "workspace-inventory-lifecycle-readiness:retire-console",
            "readiness_digest": digest({"readiness": "retire-console"}),
            "applied_at": "2026-10-12T04:35:00Z",
        }
        retired = {
            "record_version": 2,
            "record_digest": digest(product),
            "posture": "retired",
        }
        retire_event = self.lifecycle_event(
            sequence=1,
            action="retire",
            idempotency_key="retire-console",
            before=before,
            after=retired,
            previous_event_ref=None,
        )
        events.append(retire_event)
        latest_event = retire_event

        if action == "restore":
            previous_ref = {
                "id": retire_event["event_id"],
                "digest": retire_event["event_digest"],
            }
            product["posture"] = "active"
            product["record"]["version"] = 3
            product["record"]["last_mutation"] = {
                "id": "workspace-inventory-lifecycle:restore-console",
                "action": "restore",
                "idempotency_key": "restore-console",
                "request_ref": "workspace-inventory-lifecycle-request:restore-console",
                "request_digest": digest({"request": "restore-console"}),
                "readiness_ref": "workspace-inventory-lifecycle-readiness:restore-console",
                "readiness_digest": digest({"readiness": "restore-console"}),
                "applied_at": "2026-10-12T04:40:00Z",
            }
            restored = {
                "record_version": 3,
                "record_digest": digest(product),
                "posture": "active",
            }
            latest_event = self.lifecycle_event(
                sequence=2,
                action="restore",
                idempotency_key="restore-console",
                before=retired,
                after=restored,
                previous_event_ref=previous_ref,
            )
            events.append(latest_event)

        self.products["products"]["governance-operations-console"] = product
        self.history = {"schema_version": 1, "events": events}
        self._write(
            self.workspace,
            "contracts/products.yaml",
            yaml.safe_dump(self.products, sort_keys=False).encode(),
        )
        self._write(
            self.workspace,
            "contracts/workspace-inventory-history.yaml",
            yaml.safe_dump(self.history, sort_keys=False).encode(),
        )
        self._commit(self.workspace, f"Apply product {action} lifecycle state")
        self.authority_revision = self._git(self.workspace, "rev-parse", "HEAD")

        manifest = self.owner_manifest()
        manifest.update(
            schema_version=2,
            manifest_id=(
                "product-publication-manifest:governance-operations-console:"
                f"{product['record']['version']}"
            ),
            product_record_ref=(
                "workspace-governance://products/governance-operations-console/"
                f"v{product['record']['version']}"
            ),
            product_record_version=product["record"]["version"],
            lifecycle_state=product["posture"],
        )
        manifest["lifecycle_evidence"]["project_phase"] = (
            "retired" if action == "archive" else "operational"
        )
        manifest["lifecycle_evidence"]["publication_posture"] = "unlisted"
        manifest["inventory_lifecycle_binding"] = {
            "action": latest_event["action"],
            "receipt_ref": {
                "id": (
                    "workspace-inventory-lifecycle-receipt:"
                    f"{latest_event['idempotency_key']}:review-branch"
                ),
                "digest": digest({"receipt": latest_event["idempotency_key"]}),
            },
            "history_event_ref": {
                "id": latest_event["event_id"],
                "digest": latest_event["event_digest"],
                "sequence": latest_event["sequence"],
            },
            "previous_event_ref": previous_ref,
            "after": copy.deepcopy(latest_event["after"]),
        }
        self.manifest = manifest
        self._write(
            self.owner,
            "product-publication/manifest.json",
            json.dumps(self.manifest, indent=2).encode(),
        )
        self._commit(self.owner, f"Bind product {action} lifecycle evidence")
        self.manifest_revision = self._git(self.owner, "rev-parse", "HEAD")
        self._rebuild_runtime()

    def lifecycle_event(
        self,
        *,
        sequence: int,
        action: str,
        idempotency_key: str,
        before: dict,
        after: dict,
        previous_event_ref: dict | None,
    ) -> dict:
        event = {
            "event_id": (
                "workspace-inventory-event:product:governance-operations-console:"
                f"{sequence}"
            ),
            "sequence": sequence,
            "target": {
                "kind": "product",
                "name": "governance-operations-console",
                "record_id": "product:governance-operations-console",
            },
            "action": action,
            "idempotency_key": idempotency_key,
            "before": copy.deepcopy(before),
            "after": copy.deepcopy(after),
            "request_ref": {
                "id": f"workspace-inventory-lifecycle-request:{idempotency_key}",
                "digest": digest({"request": idempotency_key}),
            },
            "readiness_ref": {
                "id": f"workspace-inventory-lifecycle-readiness:{idempotency_key}",
                "digest": digest({"readiness": idempotency_key}),
            },
            "previous_event_ref": copy.deepcopy(previous_event_ref),
            "operator_ref": "operator:test",
            "applied_at": f"2026-10-12T04:{34 + sequence * 5}:00Z",
        }
        event["event_digest"] = digest(event)
        return event

    def _rebuild_runtime(self) -> None:
        self.contracts = replace(
            ProductPublicationContracts.load(),
            manifest={
                **ProductPublicationContracts.load().manifest,
                "authority_commit": self.authority_revision,
                "authority_files": {
                    "contracts/product-publication.yaml": hashlib.sha256(self.policy_raw).hexdigest(),
                    "contracts/schemas/product-publication-manifest.schema.json": hashlib.sha256(self.schema_raw).hexdigest(),
                    "contracts/schemas/workspace-inventory-history.schema.json": hashlib.sha256(self.history_schema_raw).hexdigest(),
                },
            },
        )
        self.authority = ProductPublicationAuthority(
            self.workspace,
            {"governance-operations-console": self.owner},
            self.contracts,
            trusted_ref="refs/heads/main",
        )
        self.service = ProductPublicationReadinessService(
            session_factory=self.sessions,
            authority=self.authority,
            service_identity_ref="spiffe://test/product-publication",
            implementation_ref="1" * 40,
            clock=lambda: datetime(2026, 10, 12, 5, 0, tzinfo=timezone.utc),
        )
        self.client.close()
        self.client = TestClient(
            create_app(
                product_publication_readiness=self.service,
                artifact_registry_authorizer=ArtifactRegistryAuthorizer(
                    oos_secret="o" * 32,
                    reconciler_secret="r" * 32,
                ),
            )
        )
        self.addCleanup(self.client.close)

    def envelope(self, *, schema_version: int = 1, action: str = "publish") -> dict:
        target = {
            "posture": "internal",
            "listing_scope": "workspace-portfolio",
            "audience": "workspace-operators",
            "access_class": "workspace-authenticated",
        }
        current = "unlisted"
        expected_version = 0
        previous_receipt = None
        if action == "archive":
            target = {
                "posture": "archived",
                "listing_scope": "historical-archive",
                "audience": "historical",
                "access_class": "historical-read-only",
            }
            expected_version = 1
            previous_receipt = {
                "receipt_id": "product-publication-receipt:console:1",
                "receipt_digest": digest({"publication": 1}),
                "publication_version": 1,
                "publication_posture": "unlisted",
            }
        if action == "restore":
            current = "archived"
            target = {
                "posture": "unlisted",
                "listing_scope": None,
                "audience": None,
                "access_class": None,
            }
            expected_version = 2
            previous_receipt = {
                "receipt_id": "product-publication-receipt:console:2",
                "receipt_digest": digest({"publication": 2}),
                "publication_version": 2,
                "publication_posture": "archived",
            }
        request = {
            "request_id": f"product-publication-request:console:{action}",
            "idempotency_key": f"product-publication:console:{action}",
            "action": action,
            "expected_publication_version": expected_version,
            "current_publication_posture": current,
            "target_publication": target,
            "previous_receipt": previous_receipt,
            "requested_at": "2026-10-12T04:45:00Z",
        }
        request["request_digest"] = artifact_digest(request, "request_digest")
        envelope = {
            "schema_version": schema_version,
            "artifact_type": "wgcf-product-publication-evaluation",
            "evaluation_id": f"product-publication-evaluation:console:{action}",
            "session_ref": "session:console:1",
            "execution_ref": "execution:console:1",
            "authority_revision": self.authority_revision,
            "manifest_source": {
                "repo": "governance-operations-console",
                "path": "product-publication/manifest.json",
                "source_revision": self.source_revision,
                "manifest_revision": self.manifest_revision,
                "manifest_digest": digest(self.manifest),
            },
            "request": request,
        }
        return self.bind(envelope)

    @staticmethod
    def bind(envelope: dict) -> dict:
        envelope["request"]["request_digest"] = artifact_digest(envelope["request"], "request_digest")
        envelope["evaluation_digest"] = artifact_digest(envelope, "evaluation_digest")
        return envelope

    def post(self, value: dict):
        return self.client.post(
            "/v1/readiness/product-publication",
            content=canonical_bytes(value),
            headers=self.headers,
        )

    def test_ready_replay_readback_and_non_mutation(self) -> None:
        before = self._git(self.owner, "status", "--porcelain"), self._git(self.owner, "rev-parse", "HEAD")
        value = self.envelope()
        response = self.post(value)
        self.assertEqual(response.status_code, 200, response.text)
        readiness = response.json()["readiness"]
        self.assertEqual(readiness["outcome"], "ready", readiness["findings"])
        self.assertEqual(readiness["manifest_binding"]["owner_revision"], self.source_revision)
        self.assertEqual(readiness["manifest_binding"]["manifest_revision"], self.manifest_revision)
        self.assertEqual(readiness["readiness_digest"], artifact_digest(readiness, "readiness_digest"))
        replay = self.post(value).json()
        self.assertEqual(replay["ledger"]["resolution"], "reused")
        token = readiness["readiness_digest"].removeprefix("sha256:")
        read = self.client.get(f"/v1/readiness/product-publication/{token}", headers=self.headers)
        self.assertEqual(read.json()["readiness"], readiness)
        self.assertEqual((self._git(self.owner, "status", "--porcelain"), self._git(self.owner, "rev-parse", "HEAD")), before)
        with self.sessions() as session:
            self.assertEqual(len(session.scalars(select(ProductPublicationReadinessRecord)).all()), 1)
            self.assertEqual(len(session.scalars(select(LedgerEvent)).all()), 3)

    def test_schema_v2_archive_binds_exact_retirement_history(self) -> None:
        self.install_lifecycle_state("archive")
        before = (
            self._git(self.workspace, "status", "--porcelain"),
            self._git(self.owner, "status", "--porcelain"),
        )
        response = self.post(self.envelope(schema_version=2, action="archive"))
        self.assertEqual(response.status_code, 200, response.text)
        readiness = response.json()["readiness"]
        self.assertEqual("ready", readiness["outcome"], readiness["findings"])
        self.assertEqual(2, readiness["schema_version"])
        self.assertEqual("archive", readiness["action"])
        self.assertEqual(
            self.manifest["inventory_lifecycle_binding"],
            readiness["inventory_lifecycle_binding"],
        )
        self.assertEqual(digest(self.history), readiness["authority_binding"]["history_digest"])
        self.assertEqual(
            before,
            (
                self._git(self.workspace, "status", "--porcelain"),
                self._git(self.owner, "status", "--porcelain"),
            ),
        )

    def test_schema_v2_restore_is_archived_to_unlisted_only(self) -> None:
        self.install_lifecycle_state("restore")
        value = self.envelope(schema_version=2, action="restore")
        response = self.post(value)
        self.assertEqual(response.status_code, 200, response.text)
        readiness = response.json()["readiness"]
        self.assertEqual("ready", readiness["outcome"], readiness["findings"])
        self.assertEqual("archived", readiness["current_publication_posture"])
        self.assertEqual("unlisted", readiness["target_publication"]["posture"])
        self.assertEqual("archived", readiness["previous_receipt"]["publication_posture"])
        token = readiness["readiness_digest"].removeprefix("sha256:")
        readback = self.client.get(
            f"/v1/readiness/product-publication/{token}", headers=self.headers
        )
        self.assertEqual(readiness, readback.json()["readiness"])
        self.assertEqual("reused", self.post(value).json()["ledger"]["resolution"])
        conflict = copy.deepcopy(value)
        conflict["evaluation_id"] = "product-publication-evaluation:console:restore-conflict"
        conflict["request"]["previous_receipt"]["receipt_digest"] = digest(
            {"publication": "conflict"}
        )
        self.bind(conflict)
        self.assertEqual(409, self.post(conflict).status_code)

    def test_schema_v2_rejects_contradictory_history_and_listed_restore(self) -> None:
        self.install_lifecycle_state("restore")
        self.manifest["inventory_lifecycle_binding"]["history_event_ref"]["digest"] = (
            "sha256:" + "f" * 64
        )
        self._write(
            self.owner,
            "product-publication/manifest.json",
            json.dumps(self.manifest, indent=2).encode(),
        )
        self._commit(self.owner, "Commit contradictory lifecycle binding")
        self.manifest_revision = self._git(self.owner, "rev-parse", "HEAD")
        value = self.envelope(schema_version=2, action="restore")
        response = self.post(value)
        self.assertEqual(response.status_code, 200, response.text)
        readiness = response.json()["readiness"]
        self.assertEqual("blocked", readiness["outcome"])
        self.assertIn(
            "lifecycle-event-digest-mismatch",
            [finding["code"] for finding in readiness["findings"]],
        )

        self.manifest["inventory_lifecycle_binding"]["history_event_ref"] = {
            "id": "workspace-inventory-event:product:governance-operations-console:99",
            "digest": digest({"missing": "history"}),
            "sequence": 99,
        }
        self._write(
            self.owner,
            "product-publication/manifest.json",
            json.dumps(self.manifest, indent=2).encode(),
        )
        self._commit(self.owner, "Commit stale lifecycle binding")
        self.manifest_revision = self._git(self.owner, "rev-parse", "HEAD")
        stale = self.envelope(schema_version=2, action="restore")
        stale["evaluation_id"] = "product-publication-evaluation:console:stale-history"
        stale["request"]["request_id"] = "product-publication-request:console:stale-history"
        stale["request"]["idempotency_key"] = "product-publication:console:stale-history"
        self.bind(stale)
        stale_readiness = self.post(stale).json()["readiness"]
        self.assertEqual("stale", stale_readiness["outcome"])
        self.assertIn(
            "stale-lifecycle-history",
            [finding["code"] for finding in stale_readiness["findings"]],
        )

        listed = self.envelope(schema_version=2, action="restore")
        listed["evaluation_id"] = "product-publication-evaluation:console:listed-restore"
        listed["request"]["request_id"] = "product-publication-request:console:listed-restore"
        listed["request"]["idempotency_key"] = "product-publication:console:listed-restore"
        listed["request"]["target_publication"] = {
            "posture": "internal",
            "listing_scope": "workspace-portfolio",
            "audience": "workspace-operators",
            "access_class": "workspace-authenticated",
        }
        self.bind(listed)
        self.assertEqual(422, self.post(listed).status_code)

    def test_blocked_stale_conflict_and_auth_boundaries(self) -> None:
        invalid = self.envelope()
        invalid["manifest_source"]["manifest_digest"] = "sha256:" + "f" * 64
        self.bind(invalid)
        readiness = self.post(invalid).json()["readiness"]
        self.assertEqual(readiness["outcome"], "blocked")
        self.assertIn("manifest-digest-mismatch", [item["code"] for item in readiness["findings"]])

        conflict = self.envelope()
        conflict["evaluation_id"] = "product-publication-evaluation:console:conflict"
        conflict["request"]["target_publication"] = {
            "posture": "client",
            "listing_scope": "client-portfolio",
            "audience": "authorized-clients",
            "access_class": "client-authorized",
        }
        self.bind(conflict)
        self.assertEqual(self.post(conflict).status_code, 409)

        self._write(self.owner, "README.md", b"new owner head\n")
        self._commit(self.owner, "Advance owner")
        stale = self.envelope()
        stale["evaluation_id"] = "product-publication-evaluation:console:stale"
        stale["request"]["request_id"] = "product-publication-request:console:stale"
        stale["request"]["idempotency_key"] = "product-publication:console:stale"
        self.bind(stale)
        stale_readiness = self.post(stale).json()["readiness"]
        self.assertEqual(stale_readiness["outcome"], "stale")
        self.assertIn("stale-owner-manifest", [item["code"] for item in stale_readiness["findings"]])

        self.assertEqual(self.client.post("/v1/readiness/product-publication", json=stale).status_code, 401)
        self.assertEqual(
            self.client.post(
                "/v1/readiness/product-publication",
                content=b"x" * (256 * 1024 + 1),
                headers=self.headers,
            ).status_code,
            413,
        )

        token = readiness["readiness_digest"].removeprefix("sha256:")
        other = {
            "x-wgcf-caller-id": "workspace-governance-control-fabric",
            "x-wgcf-caller-secret": "r" * 32,
        }
        self.assertEqual(
            self.client.get(
                f"/v1/readiness/product-publication/{token}", headers=other
            ).status_code,
            404,
        )
        with self.sessions.begin() as session:
            row = session.get(
                ProductPublicationReadinessRecord,
                invalid["evaluation_id"],
            )
            row.readiness = {**row.readiness, "outcome": "ready"}
        self.assertEqual(
            self.client.get(
                f"/v1/readiness/product-publication/{token}", headers=self.headers
            ).status_code,
            503,
        )

    def test_runtime_activation_still_requires_platform_authority_roots(self) -> None:
        self.assertTrue(ProductPublicationContracts.load().manifest["runtime_activation"])
        with patch.dict(
            "os.environ",
            {"WGCF_RUNTIME_PROFILE": "dev-integration", "WGCF_PRODUCT_PUBLICATION_READINESS_ENABLED": "true"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                ProductPublicationUnavailable,
                "authority roots are not configured",
            ):
                build_product_publication_readiness_runtime()

    def test_bundle_pins_approved_lifecycle_authority(self) -> None:
        manifest = ProductPublicationContracts.load().manifest
        self.assertEqual(
            manifest["authority_commit"],
            "fab38a8266512b0004081039b017904c9c55f288",
        )
        self.assertEqual(
            manifest["authority_files"]["contracts/product-publication.yaml"],
            "e2df9581fd31237a451c65c54b7398f12d055c667f1efd9ad634736b72b905ec",
        )

    @staticmethod
    def _init_repo(path: Path) -> None:
        path.mkdir(parents=True)
        subprocess.run(["git", "-C", str(path), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "Publication Test"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "publication@example.invalid"], check=True)

    @staticmethod
    def _write(repo: Path, relative: str, raw: bytes) -> None:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    def _commit(self, repo: Path, message: str) -> None:
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-m", message)
