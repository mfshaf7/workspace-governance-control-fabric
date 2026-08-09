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
            "content-address-preserving backup and restore",
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
            ["backup", "restore"],
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

    def test_restore_preflight_rejects_manifest_object_tampering(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-restore-") as temp_dir:
            state_root = Path(temp_dir) / "governance-control-fabric/test-operator"
            backup = state_root / "backups/evidence.tar.gz"
            backup.parent.mkdir(parents=True)
            body = b"evidence-body"
            with tarfile.open(backup, "w:gz") as bundle:
                member = tarfile.TarInfo("artifact/evidence.json")
                member.size = len(body)
                bundle.addfile(member, io.BytesIO(body))
            manifest = {
                "archive_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                "backup_path": str(backup.resolve()),
                "bucket": "wgcf-delivery-art-evidence",
                "credentials_included": False,
                "kubernetes_namespace": "devint-governance-control-fabric-test",
                "objects": [
                    {
                        "object_key": "artifact/evidence.json",
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "size": len(body),
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
            profile["security"]["activation_review_refs"][0]["content_sha256"] = (
                hashlib.sha256(review_body).hexdigest()
            )
            profile["security"]["activation_review_refs"][0]["source_commit"] = "a" * 40
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
            self.assertNotEqual(changed.returncode, 0)
            self.assertIn("pinned digest", changed.stderr)

            review_path.unlink()
            denied = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("Security review is unavailable", denied.stderr)

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
                "actions": ["up", "backup", "restore"],
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

            registered_profile["actions"].remove("restore")
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
