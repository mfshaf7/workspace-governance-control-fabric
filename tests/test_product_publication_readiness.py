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
        policy_raw = yaml.safe_dump(policy, sort_keys=False).encode()
        schema_raw = json.dumps(self.manifest_schema(), sort_keys=True).encode()
        self._write(self.workspace, "contracts/product-publication.yaml", policy_raw)
        self._write(
            self.workspace,
            "contracts/schemas/product-publication-manifest.schema.json",
            schema_raw,
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
                    "contracts/product-publication.yaml": hashlib.sha256(policy_raw).hexdigest(),
                    "contracts/schemas/product-publication-manifest.schema.json": hashlib.sha256(schema_raw).hexdigest(),
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
            "additionalProperties": True,
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

    def envelope(self) -> dict:
        request = {
            "request_id": "product-publication-request:console:1",
            "idempotency_key": "product-publication:console:1",
            "action": "publish",
            "expected_publication_version": 0,
            "current_publication_posture": "unlisted",
            "target_publication": {
                "posture": "internal",
                "listing_scope": "workspace-portfolio",
                "audience": "workspace-operators",
                "access_class": "workspace-authenticated",
            },
            "previous_receipt": None,
            "requested_at": "2026-10-12T04:45:00Z",
        }
        request["request_digest"] = artifact_digest(request, "request_digest")
        envelope = {
            "schema_version": 1,
            "artifact_type": "wgcf-product-publication-evaluation",
            "evaluation_id": "product-publication-evaluation:console:1",
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
