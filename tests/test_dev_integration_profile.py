from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from unittest import TestCase

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = REPO_ROOT / "dev-integration/profiles/governance-control-fabric"
SCRIPTS_ROOT = PROFILE_ROOT / "scripts"
ISOLATION_MODULE_PATH = SCRIPTS_ROOT / "lib/verify_storage_isolation.py"
ISOLATION_SPEC = importlib.util.spec_from_file_location(
    "verify_storage_isolation",
    ISOLATION_MODULE_PATH,
)
assert ISOLATION_SPEC and ISOLATION_SPEC.loader
ISOLATION_MODULE = importlib.util.module_from_spec(ISOLATION_SPEC)
ISOLATION_SPEC.loader.exec_module(ISOLATION_MODULE)
VERSIONING_MODULE_PATH = SCRIPTS_ROOT / "lib/verify_storage_versioning.py"
VERSIONING_SPEC = importlib.util.spec_from_file_location(
    "verify_storage_versioning",
    VERSIONING_MODULE_PATH,
)
assert VERSIONING_SPEC and VERSIONING_SPEC.loader
VERSIONING_MODULE = importlib.util.module_from_spec(VERSIONING_SPEC)
VERSIONING_SPEC.loader.exec_module(VERSIONING_MODULE)


class FakeVersionedStorage:
    bucket = "wgcf-delivery-art-evidence"

    def __init__(self, body: bytes) -> None:
        self.versions = [("version-1", body)]

    def get(self, object_key: str, *, version_id: str | None = None) -> tuple[bytes, str]:
        del object_key
        if version_id is None:
            return self.versions[-1][1], self.versions[-1][0]
        for candidate_version, body in self.versions:
            if candidate_version == version_id:
                return body, candidate_version
        raise AssertionError(f"unknown object version: {version_id}")

    def put(self, object_key: str, body: bytes) -> str:
        del object_key
        version_id = f"version-{len(self.versions) + 1}"
        self.versions.append((version_id, body))
        return version_id

    def delete_is_denied(self, object_key: str) -> bool:
        del object_key
        return True


class FakeUnversionedStorage(FakeVersionedStorage):
    def __init__(self, body: bytes) -> None:
        self.unversioned_body = body
        self.versions = []

    def get(self, object_key: str, *, version_id: str | None = None) -> tuple[bytes, str]:
        if not self.versions and version_id is None:
            return self.unversioned_body, ""
        return super().get(object_key, version_id=version_id)


class DevIntegrationProfileTests(TestCase):
    def test_profile_declares_k3s_api_runtime(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))

        self.assertEqual(profile["profile_id"], "governance-control-fabric")
        self.assertEqual(profile["runtime"]["platform"], "local-k3s")
        self.assertEqual(profile["runtime"]["state_model"], "persistent")
        self.assertIn("workspace-governance-control-fabric-postgresql", profile["runtime"]["components"])
        self.assertIn(
            "workspace-governance-control-fabric-object-storage",
            profile["runtime"]["components"],
        )
        self.assertIn(
            "workspace-governance-control-fabric-temporal-activity",
            profile["runtime"]["components"],
        )
        self.assertEqual(profile["testing"]["smoke"]["mutation_mode"], "read-only")
        self.assertIn("API readiness", profile["stage_handoff"]["required_checks"])
        self.assertIn("database migration", profile["stage_handoff"]["required_checks"])
        self.assertIn(
            "receipt and ledger metadata read",
            profile["stage_handoff"]["required_checks"],
        )
        self.assertIn("backup", profile["commands"])
        self.assertIn("restore", profile["commands"])
        self.assertIn(
            "content-address-preserving backup and receipt-rebinding restore",
            profile["stage_handoff"]["required_checks"],
        )
        self.assertEqual(
            profile["security"]["activation_review_refs"],
            [
                {
                    "repo": "security-architecture",
                    "path": "docs/reviews/components/2026-08-09-art-evidence-custody-and-source-provenance.md",
                    "source_commit": "2ad9700c86dfd3a762bcfdb2aba17adbc814ce43",
                    "content_sha256": "d0a16096a9ac3f26c85dbeca68364a566aeb9817cd56f7e730995db8ae367158",
                },
            ],
        )
        self.assertEqual(
            profile["authority"]["activation_contract"]["platform_acceptance_ref"],
            "repo://platform-engineering/docs/records/change-records/2026-08-09-wgcf-devint-evidence-storage.md",
        )
        self.assertEqual(
            profile["authority"]["activation_contract"]["required_actions"],
            ["up", "smoke", "down", "reset", "backup", "restore"],
        )
        self.assertFalse((SCRIPTS_ROOT / "_proposed-profile.sh").exists())

    def test_profile_common_renders_kubernetes_runtime_manifest(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-profile-") as temp_dir:
            state_root = Path(temp_dir)
            session_file = state_root / "current-session.yaml"
            session_file.write_text(
                "\n".join(
                    [
                        "schema_version: 1",
                        "lane: dev-integration",
                        "profile_id: governance-control-fabric",
                    ],
                ),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "DEVINT_NAMESPACE": "devint-governance-control-fabric-test",
                "DEVINT_OPERATOR": "test-operator",
                "DEVINT_OWNER_REPO_ROOT": str(REPO_ROOT),
                "DEVINT_PROFILE_ID": "governance-control-fabric",
                "DEVINT_PROFILE_FILE": str(PROFILE_ROOT / "profile.yaml"),
                "DEVINT_PROFILE_JSON": json.dumps(profile),
                "DEVINT_PROMOTION_REPORT": str(state_root / "promotion-report.yaml"),
                "DEVINT_SESSION_ARCHIVE": str(state_root / "sessions/session.yaml"),
                "DEVINT_SESSION_FILE": str(session_file),
                "DEVINT_STATE_ROOT": str(state_root),
                "DEVINT_WGCF_IMAGE": "ghcr.io/mfshaf7/workspace-governance-control-fabric:sha-test",
                "DEVINT_WGCF_TEMPORAL_WORKER_IMAGE": (
                    "ghcr.io/mfshaf7/workspace-governance-control-fabric-worker:sha-test"
                ),
                "DEVINT_WORKSPACE_ROOT": str(REPO_ROOT.parent),
            }

            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    (
                        f"source {SCRIPTS_ROOT / 'common.sh'}; "
                        "ensure_storage_credentials; "
                        "render_runtime_manifest; "
                        "write_temporal_worker_status; "
                        "write_access_file"
                    ),
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=40,
                check=False,
            )
            if result.returncode != 0:
                sys.stderr.write(result.stdout)
                sys.stderr.write(result.stderr)
            self.assertEqual(result.returncode, 0)

            manifest = (state_root / "rendered/wgcf-api-runtime.yaml").read_text(encoding="utf-8")
            access = (state_root / "access.txt").read_text(encoding="utf-8")
            worker_status = (
                state_root / "temporal-activity-worker-status.txt"
            ).read_text(encoding="utf-8")

            self.assertIn("kind: Deployment", manifest)
            self.assertIn("kind: StatefulSet", manifest)
            self.assertIn("kind: Service", manifest)
            self.assertIn("workspace-governance-control-fabric-postgresql", manifest)
            self.assertIn("WGCF_DATABASE_URL", manifest)
            self.assertIn("value: /var/lib/postgresql/data/pgdata", manifest)
            self.assertIn("image: ghcr.io/mfshaf7/workspace-governance-control-fabric:sha-test", manifest)
            self.assertIn("runAsNonRoot: true", manifest)
            self.assertIn("allowPrivilegeEscalation: false", manifest)
            self.assertEqual(
                manifest.count("devint.workspace/storage-credentials-sha256:"),
                2,
            )
            self.assertIn(
                "name: workspace-governance-control-fabric-temporal-activity",
                manifest,
            )
            self.assertIn(
                "image: ghcr.io/mfshaf7/workspace-governance-control-fabric-worker:sha-test",
                manifest,
            )
            self.assertIn("replicas: 0", manifest)
            self.assertIn("name: temporal-wgcf-activity", manifest)
            self.assertIn(
                "orchestration.workspace/identity: wgcf-activity-worker",
                manifest,
            )
            self.assertIn(
                "value: \"wgcf.validation-readiness.v1\"",
                manifest,
            )
            self.assertIn("mountPath: /workspace", manifest)
            self.assertIn("readOnly: true", manifest)
            self.assertIn("service: workspace-governance-control-fabric-api", access)
            self.assertIn("postgres_service: workspace-governance-control-fabric-postgresql", access)
            self.assertIn(
                "storage_service: workspace-governance-control-fabric-object-storage",
                access,
            )
            self.assertIn("enabled: false", worker_status)
            self.assertIn("replicas: 0", worker_status)

            documents = [item for item in yaml.safe_load_all(manifest) if item]
            by_kind_name = {
                (item["kind"], item["metadata"]["name"]): item
                for item in documents
            }
            api = by_kind_name[("Deployment", "workspace-governance-control-fabric-api")]
            storage = by_kind_name[
                ("StatefulSet", "workspace-governance-control-fabric-object-storage")
            ]
            api_env = {
                item["name"]: item
                for item in api["spec"]["template"]["spec"]["containers"][0]["env"]
            }
            storage_env = {
                item["name"]: item
                for item in storage["spec"]["template"]["spec"]["containers"][0]["env"]
            }
            self.assertEqual(
                api_env["WGCF_EVIDENCE_STORAGE_ACCESS_KEY"]["valueFrom"]["secretKeyRef"]["name"],
                "workspace-governance-control-fabric-object-storage-api",
            )
            self.assertNotIn("MINIO_ROOT_USER", api_env)
            self.assertEqual(
                storage_env["MINIO_ROOT_USER"]["valueFrom"]["secretKeyRef"]["name"],
                "workspace-governance-control-fabric-object-storage-root",
            )
            self.assertNotIn("WGCF_EVIDENCE_STORAGE_ACCESS_KEY", storage_env)
            self.assertIn(
                ("NetworkPolicy", "workspace-governance-control-fabric-object-storage-ingress"),
                by_kind_name,
            )
            self.assertNotIn("operator-orchestration-service", manifest)
            self.assertNotIn("openproject", manifest.lower())
            credentials = (state_root / "storage-credentials.env").read_text(encoding="utf-8")
            self.assertNotIn(credentials.split("STORAGE_ROOT_PASSWORD=", 1)[1].splitlines()[0], manifest)
            self.assertNotIn(credentials.split("STORAGE_APP_SECRET_KEY=", 1)[1].splitlines()[0], manifest)

    def test_storage_lifecycle_keeps_destructive_authority_explicit(self) -> None:
        storage_source = (SCRIPTS_ROOT / "lib/storage.sh").read_text(encoding="utf-8")
        common_source = (SCRIPTS_ROOT / "common.sh").read_text(encoding="utf-8")
        reset_source = (SCRIPTS_ROOT / "reset.sh").read_text(encoding="utf-8")
        restore_source = (SCRIPTS_ROOT / "restore.sh").read_text(encoding="utf-8")
        deploy_source = common_source.split("deploy_api() {", 1)[1].split("\n}", 1)[0]

        self.assertIn('"s3:GetObject","s3:GetObjectVersion","s3:PutObject"', storage_source)
        self.assertNotIn("s3:DeleteObject", storage_source)
        self.assertIn("mc version enable", storage_source)
        self.assertIn("prove_storage_version_preservation", deploy_source)
        self.assertIn("verify_storage_network_enforcement", deploy_source)
        self.assertIn('"object_version_id": accepted_version_id', storage_source)
        self.assertIn("?versionId={version_query}", storage_source)
        self.assertIn('"unauthorized_pod_denied": True', storage_source)
        self.assertIn('get pods -o json', storage_source)
        self.assertIn("require_storage_authority_contract", common_source)
        for script_name in ("backup.sh", "down.sh", "reset.sh", "restore.sh", "smoke.sh"):
            source = (SCRIPTS_ROOT / script_name).read_text(encoding="utf-8")
            self.assertIn("require_storage_authority_contract", source)
        self.assertLess(
            deploy_source.index('kubectl_cmd apply -f "${RUNTIME_MANIFEST}"'),
            deploy_source.index("apply_storage_secrets"),
        )
        self.assertIn('"reset-wgcf-evidence"', reset_source)
        self.assertIn('"restore-wgcf-evidence"', restore_source)
        for script_name in ("backup.sh", "restore.sh"):
            self.assertTrue(os.access(SCRIPTS_ROOT / script_name, os.X_OK))

    def test_same_key_overwrite_preserves_receipt_bound_version(self) -> None:
        body = b'{"evidence":"accepted"}'
        expected_digest = hashlib.sha256(body).hexdigest()
        storage = FakeVersionedStorage(body)

        proof = VERSIONING_MODULE.preserve_overwrite(
            storage,
            "profile-proof/evidence-custody-v1.json",
            expected_digest,
        )

        self.assertEqual(proof["accepted_version_id"], "version-1")
        self.assertEqual(proof["overwrite_version_id"], "version-2")
        self.assertEqual(proof["restored_version_id"], "version-3")
        self.assertTrue(proof["same_key_overwrite_proved"])
        self.assertTrue(proof["accepted_version_preserved"])
        self.assertEqual(storage.get("ignored", version_id="version-1")[0], body)
        self.assertEqual(storage.get("ignored")[0], body)

        verification = VERSIONING_MODULE.verify(
            storage,
            "profile-proof/evidence-custody-v1.json",
            expected_digest,
            proof["accepted_version_id"],
        )
        self.assertEqual(verification["accepted_version_id"], "version-1")
        self.assertTrue(verification["application_credential_version_read"])
        self.assertTrue(verification["application_credential_delete_denied"])

        storage.put("ignored", b'{"evidence":"later-current-value"}')
        later_verification = VERSIONING_MODULE.verify(
            storage,
            "profile-proof/evidence-custody-v1.json",
            expected_digest,
            proof["accepted_version_id"],
        )
        self.assertEqual(later_verification["accepted_sha256"], expected_digest)
        self.assertFalse(later_verification["current_matches_accepted"])

    def test_unversioned_seed_is_materialized_before_overwrite_proof(self) -> None:
        body = b'{"evidence":"accepted"}'
        expected_digest = hashlib.sha256(body).hexdigest()
        storage = FakeUnversionedStorage(body)

        proof = VERSIONING_MODULE.preserve_overwrite(
            storage,
            "profile-proof/evidence-custody-v1.json",
            expected_digest,
        )

        self.assertTrue(proof["accepted_version_materialized"])
        self.assertEqual(proof["accepted_version_id"], "version-1")
        self.assertEqual(proof["overwrite_version_id"], "version-2")
        self.assertEqual(proof["restored_version_id"], "version-3")
        self.assertEqual(storage.get("ignored", version_id="version-1")[0], body)

    def test_existing_receipt_version_remains_stable_across_reconciliation(self) -> None:
        body = b'{"evidence":"accepted"}'
        expected_digest = hashlib.sha256(body).hexdigest()
        storage = FakeVersionedStorage(body)
        storage.put("ignored", b'{"evidence":"new-current"}')

        proof = VERSIONING_MODULE.preserve_overwrite(
            storage,
            "profile-proof/evidence-custody-v1.json",
            expected_digest,
            "version-1",
        )

        self.assertEqual(proof["accepted_version_id"], "version-1")
        self.assertEqual(storage.get("ignored", version_id="version-1")[0], body)
        self.assertEqual(storage.get("ignored")[0], body)

    def test_restore_rebinds_receipt_identity_and_restores_current_object(self) -> None:
        accepted_body = b'{"evidence":"accepted"}'
        current_body = b'{"evidence":"current"}'
        expected_digest = hashlib.sha256(accepted_body).hexdigest()
        storage = FakeVersionedStorage(b"pre-restore")
        with tempfile.TemporaryDirectory(prefix="wgcf-rebind-") as temp_dir:
            package_root = Path(temp_dir)
            (package_root / "current/profile-proof").mkdir(parents=True)
            (package_root / "receipt-bound").mkdir()
            (package_root / "receipt-records").mkdir()
            (package_root / "current/profile-proof/evidence.json").write_bytes(current_body)
            (package_root / "receipt-bound/storage-receipt.bin").write_bytes(accepted_body)
            old_ref = (
                "wgcf-storage://governance-control-fabric/"
                "wgcf-delivery-art-evidence/profile-proof/evidence.json"
                "?versionId=old-version"
            )
            receipt = {
                "schema_version": 2,
                "receipt_type": "dev-integration-storage",
                "profile_id": "governance-control-fabric",
                "bucket": "wgcf-delivery-art-evidence",
                "object_key": "profile-proof/evidence.json",
                "object_version_id": "old-version",
                "content_sha256": expected_digest,
                "storage_ref": old_ref,
            }
            (package_root / "receipt-records/storage-receipt.json").write_text(
                json.dumps(receipt),
                encoding="utf-8",
            )
            manifest = {
                "bucket": "wgcf-delivery-art-evidence",
                "receipt_bindings": [
                    {
                        "receipt_name": "storage-receipt",
                        "object_key": "profile-proof/evidence.json",
                        "prior_object_version_id": "old-version",
                        "content_sha256": expected_digest,
                        "body_archive_path": "receipt-bound/storage-receipt.bin",
                        "receipt_archive_path": "receipt-records/storage-receipt.json",
                        "current_archive_path": "current/profile-proof/evidence.json",
                    },
                ],
            }

            result = VERSIONING_MODULE.rebind_receipts(storage, package_root, manifest)

            mapping = result["receipt_rebindings"][0]
            self.assertEqual(mapping["prior_object_version_id"], "old-version")
            self.assertEqual(mapping["rebound_object_version_id"], "version-2")
            self.assertEqual(mapping["restored_current_version_id"], "version-3")
            self.assertEqual(
                storage.get("ignored", version_id="version-2")[0],
                accepted_body,
            )
            self.assertEqual(storage.get("ignored")[0], current_body)
            rebound_receipt = json.loads(
                (package_root / "rebound-receipts/storage-receipt.json").read_text(
                    encoding="utf-8",
                ),
            )
            self.assertEqual(rebound_receipt["object_version_id"], "version-2")
            self.assertEqual(
                rebound_receipt["restore_supersession"]["prior_storage_ref"],
                old_ref,
            )

    def test_restore_preflight_rejects_manifest_object_tampering(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-restore-") as temp_dir:
            state_root = Path(temp_dir) / "governance-control-fabric/test-operator"
            backup = state_root / "backups/evidence.tar.gz"
            backup.parent.mkdir(parents=True)
            body = b"evidence-body"
            receipt = {
                "schema_version": 2,
                "receipt_type": "dev-integration-storage",
                "object_key": "artifact/evidence.json",
                "object_version_id": "version-before-backup",
                "content_sha256": hashlib.sha256(body).hexdigest(),
                "storage_ref": "wgcf-storage://governance-control-fabric/wgcf-delivery-art-evidence/artifact/evidence.json?versionId=version-before-backup",
            }
            receipt_body = json.dumps(receipt).encode()
            with tarfile.open(backup, "w:gz") as bundle:
                for name, content in (
                    ("current/artifact/evidence.json", body),
                    ("receipt-bound/storage-receipt.bin", body),
                    ("receipt-records/storage-receipt.json", receipt_body),
                ):
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    bundle.addfile(member, io.BytesIO(content))
            manifest = {
                "schema_version": 2,
                "archive_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                "backup_path": str(backup.resolve()),
                "bucket": "wgcf-delivery-art-evidence",
                "credentials_included": False,
                "kubernetes_namespace": "devint-governance-control-fabric-test",
                "version_ids_preserved": False,
                "restore_requires_receipt_rebinding": True,
                "objects": [
                    {
                        "object_key": "artifact/evidence.json",
                        "archive_path": "current/artifact/evidence.json",
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "size": len(body),
                    },
                ],
                "receipt_bindings": [
                    {
                        "receipt_name": "storage-receipt",
                        "receipt_archive_path": "receipt-records/storage-receipt.json",
                        "body_archive_path": "receipt-bound/storage-receipt.bin",
                        "current_archive_path": "current/artifact/evidence.json",
                        "object_key": "artifact/evidence.json",
                        "prior_object_version_id": "version-before-backup",
                        "prior_storage_ref": receipt["storage_ref"],
                        "content_sha256": hashlib.sha256(body).hexdigest(),
                        "body_size": len(body),
                        "receipt_record_sha256": hashlib.sha256(receipt_body).hexdigest(),
                        "receipt_record_size": len(receipt_body),
                    },
                ],
                "profile_id": "governance-control-fabric",
            }
            manifest_path = Path(f"{backup}.manifest.json")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            env = {
                **os.environ,
                "DEVINT_NAMESPACE": "devint-governance-control-fabric-test",
                "DEVINT_OPERATOR": "test-operator",
                "DEVINT_OWNER_REPO_ROOT": str(REPO_ROOT),
                "DEVINT_PROFILE_ID": "governance-control-fabric",
                "DEVINT_PROFILE_FILE": str(PROFILE_ROOT / "profile.yaml"),
                "DEVINT_PROFILE_JSON": json.dumps(profile),
                "DEVINT_PROMOTION_REPORT": str(state_root / "promotion-report.yaml"),
                "DEVINT_SESSION_FILE": str(state_root / "current-session.yaml"),
                "DEVINT_STATE_ROOT": str(state_root),
                "DEVINT_WORKSPACE_ROOT": str(REPO_ROOT.parent),
            }
            command = f"source {SCRIPTS_ROOT / 'common.sh'}; validate_backup_for_restore {backup}"
            valid = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)

            archive_dir = Path(temp_dir) / "archives/governance-control-fabric/test-operator/reset-proof"
            archive_dir.mkdir(parents=True)
            archived_backup = archive_dir / backup.name
            archived_manifest = Path(f"{archived_backup}.manifest.json")
            backup.rename(archived_backup)
            manifest_path.rename(archived_manifest)
            archived_command = (
                f"source {SCRIPTS_ROOT / 'common.sh'}; "
                f"validate_backup_for_restore {archived_backup}"
            )
            archived = subprocess.run(
                ["bash", "-c", archived_command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(archived.returncode, 0, archived.stderr)

            manifest["objects"][0]["sha256"] = "0" * 64
            archived_manifest.write_text(json.dumps(manifest), encoding="utf-8")
            tampered = subprocess.run(
                ["bash", "-c", archived_command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(tampered.returncode, 0)
            self.assertIn("do not match", tampered.stderr)

    def test_reset_archives_recoverable_storage_backups(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-reset-archive-") as temp_dir:
            state_root = Path(temp_dir) / "governance-control-fabric/test-operator"
            backups_dir = state_root / "backups"
            backups_dir.mkdir(parents=True)
            backup = backups_dir / "evidence.tar.gz"
            manifest = backups_dir / "evidence.tar.gz.manifest.json"
            backup.write_bytes(b"archive")
            manifest.write_text("{}\n", encoding="utf-8")
            env = {
                **os.environ,
                "DEVINT_NAMESPACE": "devint-governance-control-fabric-test",
                "DEVINT_OPERATOR": "test-operator",
                "DEVINT_OWNER_REPO_ROOT": str(REPO_ROOT),
                "DEVINT_PROFILE_ID": "governance-control-fabric",
                "DEVINT_PROFILE_FILE": str(PROFILE_ROOT / "profile.yaml"),
                "DEVINT_PROFILE_JSON": json.dumps(profile),
                "DEVINT_PROMOTION_REPORT": str(state_root / "promotion-report.yaml"),
                "DEVINT_SESSION_FILE": str(state_root / "current-session.yaml"),
                "DEVINT_STATE_ROOT": str(state_root),
                "DEVINT_WORKSPACE_ROOT": str(REPO_ROOT.parent),
            }
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {SCRIPTS_ROOT / 'common.sh'}; archive_storage_backups",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            archive_path = Path(result.stdout.strip())
            self.assertTrue((archive_path / backup.name).is_file())
            self.assertTrue((archive_path / manifest.name).is_file())
            self.assertFalse(backup.exists())

    def test_backup_output_is_confined_to_the_archived_backups_directory(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-backup-path-") as temp_dir:
            state_root = Path(temp_dir) / "governance-control-fabric/test-operator"
            backups_dir = state_root / "backups"
            backups_dir.mkdir(parents=True)
            env = {
                **os.environ,
                "DEVINT_NAMESPACE": "devint-governance-control-fabric-test",
                "DEVINT_OPERATOR": "test-operator",
                "DEVINT_OWNER_REPO_ROOT": str(REPO_ROOT),
                "DEVINT_PROFILE_ID": "governance-control-fabric",
                "DEVINT_PROFILE_FILE": str(PROFILE_ROOT / "profile.yaml"),
                "DEVINT_PROFILE_JSON": json.dumps(profile),
                "DEVINT_PROMOTION_REPORT": str(state_root / "promotion-report.yaml"),
                "DEVINT_SESSION_FILE": str(state_root / "current-session.yaml"),
                "DEVINT_STATE_ROOT": str(state_root),
                "DEVINT_WORKSPACE_ROOT": str(REPO_ROOT.parent),
            }
            command = f"source {SCRIPTS_ROOT / 'common.sh'}; validate_backup_output_path"
            accepted_path = backups_dir / "evidence.tar.gz"
            accepted = subprocess.run(
                ["bash", "-c", f"{command} {accepted_path}"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(Path(accepted.stdout.strip()), accepted_path)

            rejected = subprocess.run(
                ["bash", "-c", f"{command} {state_root / 'custom.tar.gz'}"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("backups directory", rejected.stderr)

    def test_storage_activation_requires_routed_security_review(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-security-") as temp_dir:
            temp_root = Path(temp_dir)
            workspace_root = temp_root / "workspace"
            review_path = (
                workspace_root
                / "security-architecture/docs/reviews/components/2026-08-09-art-evidence-custody-and-source-provenance.md"
            )
            review_path.parent.mkdir(parents=True)
            review_body = b"# Approved local evidence-custody review\n"
            review_path.write_bytes(review_body)
            security_repo = workspace_root / "security-architecture"
            subprocess.run(["git", "init", "-q", str(security_repo)], check=True)
            subprocess.run(
                ["git", "-C", str(security_repo), "config", "user.name", "WGCF Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(security_repo), "config", "user.email", "wgcf@example.invalid"],
                check=True,
            )
            subprocess.run(["git", "-C", str(security_repo), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(security_repo), "commit", "-q", "-m", "security review"],
                check=True,
            )
            source_commit = subprocess.run(
                ["git", "-C", str(security_repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            profile["security"]["activation_review_refs"][0]["content_sha256"] = (
                hashlib.sha256(review_body).hexdigest()
            )
            profile["security"]["activation_review_refs"][0]["source_commit"] = source_commit
            env = {
                **os.environ,
                "DEVINT_NAMESPACE": "devint-governance-control-fabric-test",
                "DEVINT_OPERATOR": "test-operator",
                "DEVINT_OWNER_REPO_ROOT": str(REPO_ROOT),
                "DEVINT_PROFILE_ID": "governance-control-fabric",
                "DEVINT_PROFILE_FILE": str(PROFILE_ROOT / "profile.yaml"),
                "DEVINT_PROFILE_JSON": json.dumps(profile),
                "DEVINT_PROMOTION_REPORT": str(temp_root / "promotion-report.yaml"),
                "DEVINT_SESSION_FILE": str(temp_root / "current-session.yaml"),
                "DEVINT_STATE_ROOT": str(temp_root / "state"),
                "DEVINT_WORKSPACE_ROOT": str(workspace_root),
            }
            command = f"source {SCRIPTS_ROOT / 'common.sh'}; require_storage_security_review"
            accepted = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

            review_path.write_text("# Changed review\n", encoding="utf-8")
            changed = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(changed.returncode, 0, changed.stderr)

            profile["security"]["activation_review_refs"][0]["source_commit"] = "a" * 40
            env["DEVINT_PROFILE_JSON"] = json.dumps(profile)
            denied = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("unavailable at its pinned commit", denied.stderr)

            profile["security"]["activation_review_refs"][0]["source_commit"] = source_commit
            profile["security"]["activation_review_refs"][0]["content_sha256"] = "0" * 64
            env["DEVINT_PROFILE_JSON"] = json.dumps(profile)
            wrong_digest = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(wrong_digest.returncode, 0)
            self.assertIn("pinned digest", wrong_digest.stderr)

    def test_storage_activation_requires_workspace_authority_contract(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-authority-") as temp_dir:
            temp_root = Path(temp_dir)
            workspace_root = temp_root / "workspace"
            registry_path = (
                workspace_root
                / "workspace-governance/contracts/developer-integration-profiles.yaml"
            )
            registry_path.parent.mkdir(parents=True)
            acceptance_path = (
                workspace_root
                / "platform-engineering/docs/records/change-records/2026-08-09-wgcf-devint-evidence-storage.md"
            )
            acceptance_path.parent.mkdir(parents=True)
            acceptance_path.write_text("# Accepted local boundary\n", encoding="utf-8")
            registered_profile = {
                "lifecycle": "active",
                "admission": {
                    "platform_acceptance_ref": (
                        "repo://platform-engineering/docs/records/change-records/"
                        "2026-08-09-wgcf-devint-evidence-storage.md"
                    ),
                },
                "actions": ["up", "smoke", "down", "reset", "backup", "restore"],
                "stage_handoff": {
                    "required_checks": profile["authority"]["activation_contract"][
                        "required_stage_checks"
                    ],
                },
            }
            registry_path.write_text(
                yaml.safe_dump({"profiles": {"governance-control-fabric": registered_profile}}),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "DEVINT_NAMESPACE": "devint-governance-control-fabric-test",
                "DEVINT_OPERATOR": "test-operator",
                "DEVINT_OWNER_REPO_ROOT": str(REPO_ROOT),
                "DEVINT_PROFILE_ID": "governance-control-fabric",
                "DEVINT_PROFILE_FILE": str(PROFILE_ROOT / "profile.yaml"),
                "DEVINT_PROFILE_JSON": json.dumps(profile),
                "DEVINT_PROMOTION_REPORT": str(temp_root / "promotion-report.yaml"),
                "DEVINT_SESSION_FILE": str(temp_root / "current-session.yaml"),
                "DEVINT_STATE_ROOT": str(temp_root / "state"),
                "DEVINT_WORKSPACE_ROOT": str(workspace_root),
            }
            command = f"source {SCRIPTS_ROOT / 'common.sh'}; require_storage_authority_contract"
            accepted = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

            registered_profile["actions"].remove("smoke")
            registry_path.write_text(
                yaml.safe_dump({"profiles": {"governance-control-fabric": registered_profile}}),
                encoding="utf-8",
            )
            denied = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("actions are not authorized", denied.stderr)

    def test_storage_isolation_reads_every_pod_secret_projection(self) -> None:
        pod = {
            "spec": {
                "containers": [
                    {
                        "env": [
                            {
                                "valueFrom": {
                                    "secretKeyRef": {"name": "regular-env"},
                                },
                            },
                        ],
                        "envFrom": [{"secretRef": {"name": "regular-env-from"}}],
                    },
                ],
                "initContainers": [
                    {"envFrom": [{"secretRef": {"name": "init-env-from"}}]},
                ],
                "ephemeralContainers": [
                    {
                        "env": [
                            {
                                "valueFrom": {
                                    "secretKeyRef": {"name": "ephemeral-env"},
                                },
                            },
                        ],
                    },
                ],
                "volumes": [
                    {"secret": {"secretName": "secret-volume"}},
                    {
                        "projected": {
                            "sources": [{"secret": {"name": "projected-secret"}}],
                        },
                    },
                ],
                "imagePullSecrets": [{"name": "image-pull-secret"}],
            },
        }
        self.assertEqual(
            ISOLATION_MODULE.collect_secret_refs(pod),
            {
                "regular-env",
                "regular-env-from",
                "init-env-from",
                "ephemeral-env",
                "secret-volume",
                "projected-secret",
                "image-pull-secret",
            },
        )

    def test_storage_isolation_requires_controller_uid_ownership(self) -> None:
        deployment = {
            "kind": "Deployment",
            "metadata": {"name": "wgcf-api", "uid": "deployment-uid"},
        }
        replica_sets = [
            {
                "kind": "ReplicaSet",
                "metadata": {
                    "name": "wgcf-api-abc",
                    "uid": "replica-set-uid",
                    "ownerReferences": [
                        {
                            "kind": "Deployment",
                            "name": "wgcf-api",
                            "uid": "deployment-uid",
                            "controller": True,
                        },
                    ],
                },
            },
            {
                "kind": "ReplicaSet",
                "metadata": {
                    "name": "wgcf-api-spoof",
                    "uid": "spoof-replica-set-uid",
                    "ownerReferences": [
                        {
                            "kind": "Deployment",
                            "name": "wgcf-api",
                            "uid": "wrong-deployment-uid",
                            "controller": True,
                        },
                    ],
                },
            },
        ]
        controllers = ISOLATION_MODULE.api_replica_set_identities(
            replica_sets,
            deployment,
            api_name="wgcf-api",
        )
        self.assertEqual(controllers, {("wgcf-api-abc", "replica-set-uid")})

        pod = {
            "metadata": {
                "labels": {"app.kubernetes.io/component": "api"},
                "ownerReferences": [
                    {
                        "kind": "ReplicaSet",
                        "name": "wgcf-api-abc",
                        "uid": "replica-set-uid",
                        "controller": True,
                    },
                ],
            },
            "spec": {"serviceAccountName": "wgcf-api"},
        }
        common = {
            "api_replica_sets": controllers,
            "storage_controller": ("wgcf-storage", "storage-uid"),
            "provision_controller": ("wgcf-provision", "provision-uid"),
            "api_service_account": "wgcf-api",
            "storage_service_account": "wgcf-storage",
            "maintenance_service_account": "wgcf-maintenance",
        }
        self.assertEqual(ISOLATION_MODULE.classify_pod(pod, **common), "api")

        spoof = json.loads(json.dumps(pod))
        spoof["metadata"]["ownerReferences"][0]["uid"] = "spoof-replica-set-uid"
        self.assertIsNone(ISOLATION_MODULE.classify_pod(spoof, **common))

        transfer = {
            "metadata": {
                "name": "wgcf-storage-transfer",
                "labels": {"app.kubernetes.io/component": "object-storage-maintenance"},
            },
            "spec": {"serviceAccountName": "wgcf-maintenance"},
        }
        self.assertIsNone(ISOLATION_MODULE.classify_pod(transfer, **common))

    def test_storage_isolation_validates_complete_network_policy(self) -> None:
        policy = {
            "spec": {
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/name": "workspace-governance-control-fabric",
                        "app.kubernetes.io/component": "object-storage",
                        "devint.profile": "governance-control-fabric",
                    },
                },
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {
                                "podSelector": {
                                    "matchExpressions": [
                                        {
                                            "key": "app.kubernetes.io/component",
                                            "operator": "In",
                                            "values": ["api", "object-storage-maintenance"],
                                        },
                                    ],
                                },
                            },
                        ],
                        "ports": [{"protocol": "TCP", "port": 9000}],
                    },
                ],
            },
        }
        self.assertEqual(
            ISOLATION_MODULE.validate_network_policy(
                policy,
                app_label="workspace-governance-control-fabric",
                profile_id="governance-control-fabric",
            ),
            ["api", "object-storage-maintenance"],
        )

        broad_policy = json.loads(json.dumps(policy))
        broad_policy["spec"]["ingress"][0]["from"].append({"namespaceSelector": {}})
        with self.assertRaisesRegex(ValueError, "unexpected ingress subjects"):
            ISOLATION_MODULE.validate_network_policy(
                broad_policy,
                app_label="workspace-governance-control-fabric",
                profile_id="governance-control-fabric",
            )

    def test_worker_activation_requires_execution_and_security_evidence(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-profile-") as temp_dir:
            state_root = Path(temp_dir)
            env = {
                **os.environ,
                "DEVINT_NAMESPACE": "devint-governance-control-fabric-test",
                "DEVINT_OPERATOR": "test-operator",
                "DEVINT_OWNER_REPO_ROOT": str(REPO_ROOT),
                "DEVINT_PROFILE_ID": "governance-control-fabric",
                "DEVINT_PROFILE_FILE": str(PROFILE_ROOT / "profile.yaml"),
                "DEVINT_PROFILE_JSON": json.dumps(profile),
                "DEVINT_PROMOTION_REPORT": str(state_root / "promotion-report.yaml"),
                "DEVINT_SESSION_FILE": str(state_root / "current-session.yaml"),
                "DEVINT_STATE_ROOT": str(state_root),
                "DEVINT_WGCF_TEMPORAL_WORKER_ENABLED": "true",
                "DEVINT_WORKSPACE_ROOT": str(REPO_ROOT.parent),
            }

            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    (
                        f"source {SCRIPTS_ROOT / 'common.sh'}; "
                        "validate_temporal_worker_activation"
                    ),
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("explicit execution authorization", result.stderr)
