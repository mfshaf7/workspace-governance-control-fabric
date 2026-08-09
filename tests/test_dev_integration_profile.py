from __future__ import annotations

import base64
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

    def delete_is_denied(
        self,
        object_key: str,
        *,
        version_id: str | None = None,
    ) -> bool:
        del object_key
        del version_id
        return True


class FakeVersionDeleteAllowedStorage(FakeVersionedStorage):
    def delete_is_denied(
        self,
        object_key: str,
        *,
        version_id: str | None = None,
    ) -> bool:
        del object_key
        return version_id is None


class FakeMissingVersionStorage(FakeVersionedStorage):
    def get(self, object_key: str, *, version_id: str | None = None) -> tuple[bytes, str]:
        if version_id is not None:
            raise VERSIONING_MODULE.HTTPError(
                "http://storage.invalid",
                404,
                "Version not found",
                {},
                None,
            )
        return super().get(object_key, version_id=version_id)


class FakeUnversionedStorage(FakeVersionedStorage):
    def __init__(self, body: bytes) -> None:
        self.unversioned_body = body
        self.versions = []

    def get(self, object_key: str, *, version_id: str | None = None) -> tuple[bytes, str]:
        if not self.versions and version_id is None:
            return self.unversioned_body, ""
        return super().get(object_key, version_id=version_id)


class FakeDeniedStorage:
    bucket = "wgcf-delivery-art-evidence"

    def get(self, object_key: str) -> tuple[bytes, str]:
        del object_key
        raise VERSIONING_MODULE.HTTPError(
            "http://storage.invalid",
            403,
            "Access denied",
            {},
            None,
        )


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
            profile["authority"]["activation_contract"]["authority_source_commit"],
            "564a63aadbf1214da827503525ba030f38e17e79",
        )
        self.assertEqual(
            profile["authority"]["activation_contract"]["authority_content_sha256"],
            "c23f42c3040d4376af46bcec2ff53d3e6810540ab34e4b97ae00dab78a427da6",
        )
        self.assertEqual(
            profile["authority"]["activation_contract"]["platform_acceptance_ref"],
            "repo://platform-engineering/docs/records/change-records/2026-08-09-wgcf-devint-evidence-storage.md",
        )
        self.assertEqual(
            profile["authority"]["activation_contract"][
                "platform_acceptance_source_commit"
            ],
            "13d7ff1ec5e314bda49492f61cf712858cb77d65",
        )
        self.assertEqual(
            profile["authority"]["activation_contract"][
                "platform_acceptance_content_sha256"
            ],
            "509c58ae2af2d0f3c12723d2115a2f28d3be4bf03e08ba96d1aa2284e060f204",
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

            credential_path = state_root / "storage-credentials.env"
            credential_path.write_text(
                credentials.replace(
                    "STORAGE_APP_ACCESS_KEY=wgcf-evidence-api",
                    "STORAGE_APP_ACCESS_KEY=rotated-access-key",
                ),
                encoding="utf-8",
            )
            changed_identity = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {SCRIPTS_ROOT / 'common.sh'}; storage_credentials_digest",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(changed_identity.returncode, 0)
            self.assertIn("application identity is immutable", changed_identity.stderr)

    def test_storage_lifecycle_keeps_destructive_authority_explicit(self) -> None:
        storage_source = (SCRIPTS_ROOT / "lib/storage.sh").read_text(encoding="utf-8")
        common_source = (SCRIPTS_ROOT / "common.sh").read_text(encoding="utf-8")
        reset_source = (SCRIPTS_ROOT / "reset.sh").read_text(encoding="utf-8")
        restore_source = (SCRIPTS_ROOT / "restore.sh").read_text(encoding="utf-8")
        smoke_source = (SCRIPTS_ROOT / "smoke.sh").read_text(encoding="utf-8")
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
        namespace_index = deploy_source.index('kubectl_cmd create namespace "${NAMESPACE}"')
        rotation_capture_index = deploy_source.index("capture_storage_credentials_for_rotation")
        secret_index = deploy_source.index("apply_storage_secrets")
        runtime_index = deploy_source.index('kubectl_cmd apply -f "${RUNTIME_MANIFEST}"')
        rotation_proof_index = deploy_source.index("verify_retired_storage_credentials")
        receipt_index = deploy_source.index("write_storage_receipt")
        self.assertLess(rotation_capture_index, secret_index)
        self.assertLess(namespace_index, secret_index)
        self.assertLess(namespace_index, rotation_capture_index)
        self.assertLess(secret_index, runtime_index)
        self.assertLess(rotation_proof_index, receipt_index)
        self.assertIn("expect-denied-stdin", storage_source)
        self.assertIn("persist_pending_storage_credential_rotation", storage_source)
        self.assertIn("STORAGE_CREDENTIAL_RETIREMENT_SECRET", storage_source)
        self.assertIn("verify_storage_network_enforcement", smoke_source)
        self.assertNotIn("verify_storage_network_proof", smoke_source)
        self.assertIn("require_no_pending_storage_credential_rotation", smoke_source)
        self.assertIn('ln -- "${STORAGE_BACKUP_STAGING_MANIFEST}"', storage_source)
        self.assertIn('ln -- "${STORAGE_BACKUP_STAGING_ARCHIVE}"', storage_source)
        self.assertLess(
            restore_source.index("snapshot_backup_for_restore"),
            restore_source.index("validate_backup_for_restore"),
        )
        self.assertIn(
            'restore_evidence_storage "${STORAGE_RESTORE_INPUT_ARCHIVE}"',
            restore_source,
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
        self.assertTrue(verification["application_credential_version_delete_denied"])
        self.assertEqual(
            VERSIONING_MODULE.probe_receipt_version(
                storage,
                "profile-proof/evidence-custody-v1.json",
                "version-1",
            )["state"],
            "present",
        )
        self.assertEqual(
            VERSIONING_MODULE.probe_receipt_version(
                FakeMissingVersionStorage(body),
                "profile-proof/evidence-custody-v1.json",
                "version-1",
            )["state"],
            "missing",
        )

        with self.assertRaisesRegex(SystemExit, "receipt-bound version deletion"):
            VERSIONING_MODULE.verify(
                FakeVersionDeleteAllowedStorage(body),
                "profile-proof/evidence-custody-v1.json",
                expected_digest,
                "version-1",
            )

        storage.put("ignored", b'{"evidence":"later-current-value"}')
        later_verification = VERSIONING_MODULE.verify(
            storage,
            "profile-proof/evidence-custody-v1.json",
            expected_digest,
            proof["accepted_version_id"],
        )
        self.assertEqual(later_verification["accepted_sha256"], expected_digest)
        self.assertFalse(later_verification["current_matches_accepted"])

    def test_pending_credential_rotation_survives_interrupted_up(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-pending-rotation-") as temp_dir:
            state_root = Path(temp_dir)
            credentials = "\n".join(
                [
                    "STORAGE_ROOT_USER=wgcf-root",
                    "STORAGE_ROOT_PASSWORD=replacement-root-secret",
                    "STORAGE_APP_ACCESS_KEY=wgcf-evidence-api",
                    "STORAGE_APP_SECRET_KEY=replacement-app-secret",
                ]
            ) + "\n"
            (state_root / "storage-credentials.env").write_text(
                credentials,
                encoding="utf-8",
            )
            target_digest = hashlib.sha256(credentials.encode()).hexdigest()
            values = {
                "target-credentials-sha256": target_digest,
                "retired-root-user": "wgcf-root",
                "retired-root-password": "retired-root-secret",
                "retired-app-access-key": "wgcf-evidence-api",
                "retired-app-secret-key": "retired-app-secret",
            }
            pending_secret = {
                "data": {
                    key: base64.b64encode(value.encode()).decode()
                    for key, value in values.items()
                }
            }
            pending_path = state_root / "pending-secret.json"
            pending_path.write_text(json.dumps(pending_secret), encoding="utf-8")
            session_file = state_root / "current-session.yaml"
            session_file.write_text("profile_id: governance-control-fabric\n", encoding="utf-8")
            env = {
                **os.environ,
                "DEVINT_NAMESPACE": "devint-governance-control-fabric-test",
                "DEVINT_OPERATOR": "test-operator",
                "DEVINT_OWNER_REPO_ROOT": str(REPO_ROOT),
                "DEVINT_PROFILE_ID": "governance-control-fabric",
                "DEVINT_PROFILE_FILE": str(PROFILE_ROOT / "profile.yaml"),
                "DEVINT_PROFILE_JSON": json.dumps(profile),
                "DEVINT_PROMOTION_REPORT": str(state_root / "promotion-report.yaml"),
                "DEVINT_SESSION_FILE": str(session_file),
                "DEVINT_STATE_ROOT": str(state_root),
                "DEVINT_WORKSPACE_ROOT": str(REPO_ROOT.parent),
                "PENDING_SECRET_JSON": str(pending_path),
            }
            command = (
                f"source {SCRIPTS_ROOT / 'common.sh'}; "
                "kubectl_cmd() { "
                "if [[ \"$3\" == get && \"$4\" == secret && "
                "\"$5\" == \"${STORAGE_CREDENTIAL_RETIREMENT_SECRET}\" ]]; then "
                "cat \"${PENDING_SECRET_JSON}\"; else return 1; fi; }; "
                "capture_storage_credentials_for_rotation; "
                "printf '%s|%s|%s|%s|%s\n' "
                "\"${STORAGE_CREDENTIAL_ROTATION_DETECTED}\" "
                "\"${STORAGE_RETIRED_ROOT_USER}\" "
                "\"${STORAGE_RETIRED_ROOT_PASSWORD}\" "
                "\"${STORAGE_RETIRED_APP_ACCESS_KEY}\" "
                "\"${STORAGE_RETIRED_APP_SECRET_KEY}\""
            )
            result = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(),
                "true|wgcf-root|retired-root-secret|wgcf-evidence-api|retired-app-secret",
            )

    def test_missing_pending_rotation_captures_both_live_credentials(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-new-rotation-") as temp_dir:
            state_root = Path(temp_dir)
            credentials = "\n".join(
                [
                    "STORAGE_ROOT_USER=wgcf-root",
                    "STORAGE_ROOT_PASSWORD=replacement-root-secret",
                    "STORAGE_APP_ACCESS_KEY=wgcf-evidence-api",
                    "STORAGE_APP_SECRET_KEY=replacement-app-secret",
                ]
            ) + "\n"
            (state_root / "storage-credentials.env").write_text(credentials, encoding="utf-8")
            root_secret = {
                "data": {
                    "root-user": base64.b64encode(b"wgcf-root").decode(),
                    "root-password": base64.b64encode(b"retired-root-secret").decode(),
                }
            }
            app_secret = {
                "data": {
                    "access-key": base64.b64encode(b"wgcf-evidence-api").decode(),
                    "secret-key": base64.b64encode(b"retired-app-secret").decode(),
                }
            }
            root_path = state_root / "root-secret.json"
            app_path = state_root / "app-secret.json"
            applied_path = state_root / "applied-pending-secret.yaml"
            root_path.write_text(json.dumps(root_secret), encoding="utf-8")
            app_path.write_text(json.dumps(app_secret), encoding="utf-8")
            session_file = state_root / "current-session.yaml"
            session_file.write_text("profile_id: governance-control-fabric\n", encoding="utf-8")
            env = {
                **os.environ,
                "DEVINT_NAMESPACE": "devint-governance-control-fabric-test",
                "DEVINT_OPERATOR": "test-operator",
                "DEVINT_OWNER_REPO_ROOT": str(REPO_ROOT),
                "DEVINT_PROFILE_ID": "governance-control-fabric",
                "DEVINT_PROFILE_FILE": str(PROFILE_ROOT / "profile.yaml"),
                "DEVINT_PROFILE_JSON": json.dumps(profile),
                "DEVINT_PROMOTION_REPORT": str(state_root / "promotion-report.yaml"),
                "DEVINT_SESSION_FILE": str(session_file),
                "DEVINT_STATE_ROOT": str(state_root),
                "DEVINT_WORKSPACE_ROOT": str(REPO_ROOT.parent),
                "ROOT_SECRET_JSON": str(root_path),
                "APP_SECRET_JSON": str(app_path),
                "APPLIED_SECRET_YAML": str(applied_path),
            }
            command = (
                f"source {SCRIPTS_ROOT / 'common.sh'}; "
                "kubectl_cmd() { "
                "if [[ \"$1\" == apply ]]; then cat >\"${APPLIED_SECRET_YAML}\"; return 0; fi; "
                "if [[ \"$3\" == get && \"$4\" == secret ]]; then "
                "case \"$5\" in "
                "\"${STORAGE_CREDENTIAL_RETIREMENT_SECRET}\") return 0 ;; "
                "\"${STORAGE_ROOT_SECRET}\") cat \"${ROOT_SECRET_JSON}\" ;; "
                "\"${STORAGE_APP_SECRET}\") cat \"${APP_SECRET_JSON}\" ;; "
                "*) return 1 ;; esac; return 0; fi; return 1; }; "
                "capture_storage_credentials_for_rotation; "
                "printf '%s|%s|%s\n' "
                "\"${STORAGE_CREDENTIAL_ROTATION_DETECTED}\" "
                "\"${STORAGE_RETIRED_ROOT_PASSWORD}\" "
                "\"${STORAGE_RETIRED_APP_SECRET_KEY}\""
            )
            result = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout.strip(),
                "true|retired-root-secret|retired-app-secret",
            )
            applied = applied_path.read_text(encoding="utf-8")
            self.assertIn("pending-credential-retirement", applied)
            self.assertIn("target-credentials-sha256", applied)

    def test_retired_storage_credentials_must_fail_authentication(self) -> None:
        proof = VERSIONING_MODULE.assert_credentials_denied(
            FakeDeniedStorage(),
            "profile-proof/evidence-custody-v1.json",
        )

        self.assertTrue(proof["authentication_denied"])
        with self.assertRaisesRegex(SystemExit, "still authenticates"):
            VERSIONING_MODULE.assert_credentials_denied(
                FakeVersionedStorage(b"still-authorized"),
                "profile-proof/evidence-custody-v1.json",
            )

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
                "kubernetes_namespace": "devint-governance-control-fabric-test",
                "bucket": "wgcf-delivery-art-evidence",
                "object_key": "profile-proof/evidence.json",
                "object_version_id": "old-version",
                "content_sha256": expected_digest,
                "storage_ref": old_ref,
                "service_identity_ref": (
                    "kubernetes://devint-governance-control-fabric-test/"
                    "serviceaccount/workspace-governance-control-fabric-api"
                ),
                "application_secret_ref": (
                    "kubernetes://devint-governance-control-fabric-test/secret/"
                    "workspace-governance-control-fabric-object-storage-api"
                ),
            }
            (package_root / "receipt-records/storage-receipt.json").write_text(
                json.dumps(receipt),
                encoding="utf-8",
            )
            manifest = {
                "profile_id": "governance-control-fabric",
                "kubernetes_namespace": "devint-governance-control-fabric-test",
                "bucket": "wgcf-delivery-art-evidence",
                "receipt_bindings": [
                    {
                        "receipt_name": "storage-receipt",
                        "object_key": "profile-proof/evidence.json",
                        "prior_object_version_id": "old-version",
                        "prior_storage_ref": old_ref,
                        "content_sha256": expected_digest,
                        "body_archive_path": "receipt-bound/storage-receipt.bin",
                        "receipt_archive_path": "receipt-records/storage-receipt.json",
                        "current_archive_path": "current/profile-proof/evidence.json",
                    },
                ],
            }

            active_scope = {
                "profile_id": "governance-control-fabric",
                "kubernetes_namespace": "devint-governance-control-fabric-test",
                "bucket": "wgcf-delivery-art-evidence",
                "service_identity_ref": receipt["service_identity_ref"],
                "application_secret_ref": receipt["application_secret_ref"],
            }
            result = VERSIONING_MODULE.rebind_receipts(
                storage,
                package_root,
                manifest,
                active_scope,
            )

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

            wrong_scope = {**active_scope, "profile_id": "wrong-profile"}
            with self.assertRaisesRegex(SystemExit, "active storage scope"):
                VERSIONING_MODULE.rebind_receipts(
                    storage,
                    package_root,
                    manifest,
                    wrong_scope,
                )

    def test_restore_preflight_rejects_manifest_object_tampering(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-restore-") as temp_dir:
            state_root = Path(temp_dir) / "governance-control-fabric/test-operator"
            backup = state_root / "backups/evidence.tar.gz"
            backup.parent.mkdir(parents=True)
            object_key = "profile-proof/evidence-custody-v1.json"
            body = (
                b'{"artifact_class":"architecture_packet","profile":'
                b'"governance-control-fabric","proof":"dev-integration-storage-v1"}\n'
            )
            receipt = {
                "schema_version": 2,
                "receipt_type": "dev-integration-storage",
                "profile_id": "governance-control-fabric",
                "kubernetes_namespace": "devint-governance-control-fabric-test",
                "bucket": "wgcf-delivery-art-evidence",
                "object_key": object_key,
                "object_version_id": "version-before-backup",
                "content_sha256": hashlib.sha256(body).hexdigest(),
                "storage_ref": (
                    "wgcf-storage://governance-control-fabric/"
                    "wgcf-delivery-art-evidence/"
                    f"{object_key}?versionId=version-before-backup"
                ),
                "service_identity_ref": (
                    "kubernetes://devint-governance-control-fabric-test/"
                    "serviceaccount/workspace-governance-control-fabric-api"
                ),
                "application_secret_ref": (
                    "kubernetes://devint-governance-control-fabric-test/secret/"
                    "workspace-governance-control-fabric-object-storage-api"
                ),
            }
            receipt_body = json.dumps(receipt).encode()
            with tarfile.open(backup, "w:gz") as bundle:
                for name, content in (
                    (f"current/{object_key}", body),
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
                        "object_key": object_key,
                        "archive_path": f"current/{object_key}",
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "size": len(body),
                    },
                ],
                "receipt_bindings": [
                    {
                        "receipt_name": "storage-receipt",
                        "receipt_archive_path": "receipt-records/storage-receipt.json",
                        "body_archive_path": "receipt-bound/storage-receipt.bin",
                        "current_archive_path": f"current/{object_key}",
                        "object_key": object_key,
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
            original_archive = backup.read_bytes()
            original_manifest = manifest_path.read_bytes()
            snapshot = subprocess.run(
                [
                    "bash",
                    "-c",
                    (
                        f"source {SCRIPTS_ROOT / 'common.sh'}; "
                        f"snapshot_backup_for_restore {backup}; "
                        'printf "%s\\n" "${STORAGE_RESTORE_INPUT_ARCHIVE}"'
                    ),
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
            snapshot_path = Path(snapshot.stdout.strip())
            backup.write_bytes(b"changed after snapshot")
            manifest_path.write_text("{}\n", encoding="utf-8")
            self.assertEqual(snapshot_path.read_bytes(), original_archive)
            self.assertEqual(
                Path(f"{snapshot_path}.manifest.json").read_bytes(),
                original_manifest,
            )
            backup.write_bytes(original_archive)
            manifest_path.write_bytes(original_manifest)
            valid = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)

            substituted_key = "artifact/substituted.json"
            substituted_receipt = {
                **receipt,
                "object_key": substituted_key,
                "storage_ref": (
                    "wgcf-storage://governance-control-fabric/"
                    "wgcf-delivery-art-evidence/"
                    f"{substituted_key}?versionId=version-before-backup"
                ),
            }
            substituted_receipt_body = json.dumps(substituted_receipt).encode()
            with tarfile.open(backup, "w:gz") as bundle:
                for name, content in (
                    (f"current/{object_key}", body),
                    (f"current/{substituted_key}", body),
                    ("receipt-bound/storage-receipt.bin", body),
                    ("receipt-records/storage-receipt.json", substituted_receipt_body),
                    ("receipt-bound/seed-receipt.bin", body),
                    ("receipt-records/seed-receipt.json", receipt_body),
                ):
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    bundle.addfile(member, io.BytesIO(content))
            substituted_manifest = json.loads(json.dumps(manifest))
            substituted_manifest["objects"].append(
                {
                    "object_key": substituted_key,
                    "archive_path": f"current/{substituted_key}",
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "size": len(body),
                }
            )
            seed_binding = json.loads(
                json.dumps(substituted_manifest["receipt_bindings"][0])
            )
            seed_binding["receipt_name"] = "seed-receipt"
            seed_binding["receipt_archive_path"] = "receipt-records/seed-receipt.json"
            seed_binding["body_archive_path"] = "receipt-bound/seed-receipt.bin"
            substituted_manifest["receipt_bindings"].append(seed_binding)
            substituted_manifest["archive_sha256"] = hashlib.sha256(
                backup.read_bytes()
            ).hexdigest()
            substituted_binding = substituted_manifest["receipt_bindings"][0]
            substituted_binding["object_key"] = substituted_key
            substituted_binding["current_archive_path"] = f"current/{substituted_key}"
            substituted_binding["prior_storage_ref"] = substituted_receipt["storage_ref"]
            substituted_binding["receipt_record_sha256"] = hashlib.sha256(
                substituted_receipt_body
            ).hexdigest()
            substituted_binding["receipt_record_size"] = len(substituted_receipt_body)
            manifest_path.write_text(json.dumps(substituted_manifest), encoding="utf-8")
            substituted_seed = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(substituted_seed.returncode, 0)
            self.assertIn("primary storage receipt", substituted_seed.stderr)
            backup.write_bytes(original_archive)
            manifest_path.write_bytes(original_manifest)

            manifest["receipt_bindings"][0]["prior_object_version_id"] = "wrong-version"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            wrong_binding = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(wrong_binding.returncode, 0)
            self.assertIn("invalid prior storage reference", wrong_binding.stderr)
            manifest["receipt_bindings"][0]["prior_object_version_id"] = (
                "version-before-backup"
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            manifest["receipt_bindings"][0]["receipt_name"] = "nested/storage-receipt"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            unsafe_receipt_name = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(unsafe_receipt_name.returncode, 0)
            self.assertIn("invalid or duplicate receipt binding", unsafe_receipt_name.stderr)
            manifest["receipt_bindings"][0]["receipt_name"] = "storage-receipt"

            manifest["objects"][0]["object_key"] = "profile-proof//evidence-custody-v1.json"
            manifest["objects"][0]["archive_path"] = "current/profile-proof//evidence-custody-v1.json"
            manifest["receipt_bindings"][0]["object_key"] = "profile-proof//evidence-custody-v1.json"
            manifest["receipt_bindings"][0]["current_archive_path"] = (
                "current/profile-proof//evidence-custody-v1.json"
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            noncanonical_object = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(noncanonical_object.returncode, 0)
            self.assertIn("invalid or duplicate object key", noncanonical_object.stderr)
            manifest["objects"][0]["object_key"] = object_key
            manifest["objects"][0]["archive_path"] = f"current/{object_key}"
            manifest["receipt_bindings"][0]["object_key"] = object_key
            manifest["receipt_bindings"][0]["current_archive_path"] = (
                f"current/{object_key}"
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

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
            self.assertIn("configured seed digest", tampered.stderr)

            manifest["objects"][0]["sha256"] = hashlib.sha256(body).hexdigest()
            with tarfile.open(archived_backup, "w:gz") as bundle:
                for name, content in (
                    (f"current/{object_key}", body),
                    ("receipt-bound/storage-receipt.bin", body),
                    ("receipt-records/storage-receipt.json", receipt_body),
                ):
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    bundle.addfile(member, io.BytesIO(content))
                link = tarfile.TarInfo("current/linked-evidence.json")
                link.type = tarfile.SYMTYPE
                link.linkname = object_key
                bundle.addfile(link)
            manifest["archive_sha256"] = hashlib.sha256(archived_backup.read_bytes()).hexdigest()
            archived_manifest.write_text(json.dumps(manifest), encoding="utf-8")
            unsafe_member = subprocess.run(
                ["bash", "-c", archived_command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(unsafe_member.returncode, 0)
            self.assertIn("unsupported member", unsafe_member.stderr)

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
            storage_source = (
                SCRIPTS_ROOT / "lib/storage.sh"
            ).read_text(encoding="utf-8")
            self.assertIn('mv -- "${BACKUPS_DIR}" "${archive_path}"', storage_source)
            self.assertNotIn('mv -- "${backup_file}" "${archive_path}/"', storage_source)

    def test_reset_refuses_to_delete_an_incomplete_backup_pair(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-reset-orphan-") as temp_dir:
            state_root = Path(temp_dir) / "governance-control-fabric/test-operator"
            backups_dir = state_root / "backups"
            backups_dir.mkdir(parents=True)
            orphan = backups_dir / "evidence.tar.gz"
            orphan.write_bytes(b"archive")
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
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("backup manifest is missing", result.stderr)
            self.assertTrue(orphan.is_file())

    def test_restore_receipt_records_an_empty_live_store_without_fake_backup(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-empty-restore-") as temp_dir:
            state_root = Path(temp_dir) / "governance-control-fabric/test-operator"
            state_root.mkdir(parents=True)
            selected_backup = state_root / "selected.tar.gz"
            selected_backup.write_bytes(b"selected-backup")
            (state_root / "storage-receipt-rebindings.json").write_text(
                json.dumps(
                    {
                        "receipt_identity_rebound": True,
                        "receipt_rebindings": [{"receipt_name": "storage-receipt"}],
                    }
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
                "DEVINT_SESSION_FILE": str(state_root / "current-session.yaml"),
                "DEVINT_STATE_ROOT": str(state_root),
                "DEVINT_WORKSPACE_ROOT": str(REPO_ROOT.parent),
            }
            command = (
                f"source {SCRIPTS_ROOT / 'common.sh'}; "
                f"write_restore_receipt {selected_backup} '' {selected_backup} empty-live-store"
            )
            result = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(
                (state_root / "restore-receipt.json").read_text(encoding="utf-8")
            )
            self.assertEqual(receipt["pre_restore_state"], "empty-live-store")
            self.assertIsNone(receipt["pre_restore_backup"])
            self.assertIsNone(receipt["pre_restore_archive_sha256"])

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

            accepted_path.write_bytes(b"existing recovery bundle")
            existing_archive = subprocess.run(
                ["bash", "-c", f"{command} {accepted_path}"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(existing_archive.returncode, 0)
            self.assertIn("already exists", existing_archive.stderr)
            accepted_path.unlink()

            manifest_path = Path(f"{accepted_path}.manifest.json")
            manifest_path.write_text("{}\n", encoding="utf-8")
            existing_manifest = subprocess.run(
                ["bash", "-c", f"{command} {accepted_path}"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(existing_manifest.returncode, 0)
            self.assertIn("already exists", existing_manifest.stderr)

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

    def test_storage_receipt_binding_requires_exact_active_scope(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-receipt-scope-") as temp_dir:
            state_root = Path(temp_dir) / "governance-control-fabric/test-operator"
            state_root.mkdir(parents=True)
            namespace = "devint-governance-control-fabric-test"
            version_id = "version-1"
            object_key = "profile-proof/evidence-custody-v1.json"
            receipt_path = state_root / "storage-receipt.json"
            receipt = {
                "schema_version": 2,
                "receipt_type": "dev-integration-storage",
                "profile_id": "governance-control-fabric",
                "kubernetes_namespace": namespace,
                "bucket": "wgcf-delivery-art-evidence",
                "object_key": object_key,
                "object_version_id": version_id,
                "content_sha256": "a" * 64,
                "storage_ref": (
                    "wgcf-storage://governance-control-fabric/"
                    f"wgcf-delivery-art-evidence/{object_key}?versionId={version_id}"
                ),
                "service_identity_ref": (
                    f"kubernetes://{namespace}/serviceaccount/"
                    "workspace-governance-control-fabric-api"
                ),
                "application_secret_ref": (
                    f"kubernetes://{namespace}/secret/"
                    "workspace-governance-control-fabric-object-storage-api"
                ),
            }
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            env = {
                **os.environ,
                "DEVINT_NAMESPACE": namespace,
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
            command = f"source {SCRIPTS_ROOT / 'common.sh'}; read_storage_receipt_binding"
            accepted = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertEqual(accepted.stdout.splitlines(), [object_key, version_id, "a" * 64])

            receipt["bucket"] = "wrong-bucket"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            rejected = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("different bucket", rejected.stderr)

    def test_restore_receipts_publish_through_validated_atomic_staging(self) -> None:
        storage_source = (
            SCRIPTS_ROOT / "lib/storage.sh"
        ).read_text(encoding="utf-8")

        self.assertIn('staged_path="$(mktemp "${target_path}.XXXXXX.tmp")"', storage_source)
        self.assertIn('read_storage_receipt_binding "${staged_path}"', storage_source)
        self.assertIn('mv -f -- "${staged_path}" "${target_path}"', storage_source)
        self.assertIn(
            "capture_rebound_json_atomically \\\n"
            "    /transfer/restore/rebound-receipts/storage-receipt.json",
            storage_source,
        )
        self.assertNotIn(
            'cat /transfer/restore/rebound-receipts/storage-receipt.json '
            '>"${STORAGE_RECEIPT_FILE}"',
            storage_source,
        )
        self.assertIn(
            'staged_path="$(mktemp "${STORAGE_RECEIPT_FILE}.XXXXXX.tmp")"',
            storage_source,
        )
        self.assertEqual(
            storage_source.count('mv -f -- "${staged_path}" "${STORAGE_RECEIPT_FILE}"'),
            2,
        )

    def test_smoke_gate_rejects_pending_credential_retirement(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="wgcf-devint-smoke-rotation-") as temp_dir:
            temp_root = Path(temp_dir)
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
                "DEVINT_WORKSPACE_ROOT": str(REPO_ROOT.parent),
            }
            source = f"source {SCRIPTS_ROOT / 'common.sh'}; "
            pending = subprocess.run(
                [
                    "bash",
                    "-c",
                    source
                    + "kubectl_cmd() { printf '%s\\n' "
                    + "'secret/pending-storage-credential-retirement'; }; "
                    + "require_no_pending_storage_credential_rotation",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(pending.returncode, 0)
            self.assertIn("credential retirement is pending", pending.stderr)

            clear = subprocess.run(
                [
                    "bash",
                    "-c",
                    source
                    + "kubectl_cmd() { return 0; }; "
                    + "require_no_pending_storage_credential_rotation",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(clear.returncode, 0, clear.stderr)

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
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(security_repo),
                    "update-ref",
                    "refs/remotes/origin/main",
                    source_commit,
                ],
                check=True,
            )
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

            subprocess.run(["git", "-C", str(security_repo), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(security_repo), "commit", "-q", "-m", "change review"],
                check=True,
            )
            unlanded_commit = subprocess.run(
                ["git", "-C", str(security_repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            profile["security"]["activation_review_refs"][0]["source_commit"] = (
                unlanded_commit
            )
            profile["security"]["activation_review_refs"][0]["content_sha256"] = (
                hashlib.sha256(review_path.read_bytes()).hexdigest()
            )
            env["DEVINT_PROFILE_JSON"] = json.dumps(profile)
            unlanded = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(unlanded.returncode, 0)
            self.assertIn("not landed on origin/main", unlanded.stderr)

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
            self.assertIn("not landed on origin/main", denied.stderr)

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
            platform_root = workspace_root / "platform-engineering"
            subprocess.run(
                ["git", "init", "-q", str(platform_root)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(platform_root), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(platform_root), "config", "user.name", "Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(platform_root), "add", acceptance_path.relative_to(platform_root)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(platform_root), "commit", "-qm", "accept storage"],
                check=True,
            )
            acceptance_commit = subprocess.run(
                ["git", "-C", str(platform_root), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(platform_root),
                    "update-ref",
                    "refs/remotes/origin/main",
                    acceptance_commit,
                ],
                check=True,
            )
            activation_contract = profile["authority"]["activation_contract"]
            activation_contract["platform_acceptance_source_commit"] = acceptance_commit
            activation_contract["platform_acceptance_content_sha256"] = hashlib.sha256(
                acceptance_path.read_bytes()
            ).hexdigest()
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
            governance_root = workspace_root / "workspace-governance"
            subprocess.run(["git", "init", "-q", str(governance_root)], check=True)
            subprocess.run(
                ["git", "-C", str(governance_root), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(governance_root), "config", "user.name", "Test"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(governance_root), "add", registry_path.relative_to(governance_root)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(governance_root), "commit", "-qm", "authorize storage"],
                check=True,
            )
            authority_commit = subprocess.run(
                ["git", "-C", str(governance_root), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(governance_root),
                    "update-ref",
                    "refs/remotes/origin/main",
                    authority_commit,
                ],
                check=True,
            )
            activation_contract["authority_source_commit"] = authority_commit
            activation_contract["authority_content_sha256"] = hashlib.sha256(
                registry_path.read_bytes()
            ).hexdigest()
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

            acceptance_path.write_text("# Unlanded acceptance change\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(platform_root), "add", acceptance_path.relative_to(platform_root)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(platform_root), "commit", "-qm", "change acceptance"],
                check=True,
            )
            unlanded_acceptance_commit = subprocess.run(
                ["git", "-C", str(platform_root), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            activation_contract["platform_acceptance_source_commit"] = (
                unlanded_acceptance_commit
            )
            activation_contract["platform_acceptance_content_sha256"] = hashlib.sha256(
                acceptance_path.read_bytes()
            ).hexdigest()
            env["DEVINT_PROFILE_JSON"] = json.dumps(profile)
            unlanded_acceptance = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(unlanded_acceptance.returncode, 0)
            self.assertIn("not landed on origin/main", unlanded_acceptance.stderr)
            activation_contract["platform_acceptance_source_commit"] = acceptance_commit
            activation_contract["platform_acceptance_content_sha256"] = hashlib.sha256(
                b"# Accepted local boundary\n"
            ).hexdigest()
            env["DEVINT_PROFILE_JSON"] = json.dumps(profile)

            registered_profile["actions"].remove("smoke")
            registry_path.write_text(
                yaml.safe_dump({"profiles": {"governance-control-fabric": registered_profile}}),
                encoding="utf-8",
            )
            mutable_checkout = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(mutable_checkout.returncode, 0, mutable_checkout.stderr)

            subprocess.run(
                ["git", "-C", str(governance_root), "add", registry_path.relative_to(governance_root)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(governance_root), "commit", "-qm", "remove action"],
                check=True,
            )
            unlanded_commit = subprocess.run(
                ["git", "-C", str(governance_root), "rev-parse", "HEAD"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            activation_contract["authority_source_commit"] = unlanded_commit
            activation_contract["authority_content_sha256"] = hashlib.sha256(
                registry_path.read_bytes()
            ).hexdigest()
            env["DEVINT_PROFILE_JSON"] = json.dumps(profile)
            unlanded = subprocess.run(
                ["bash", "-c", command],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(unlanded.returncode, 0)
            self.assertIn("not landed on origin/main", unlanded.stderr)

            subprocess.run(
                [
                    "git",
                    "-C",
                    str(governance_root),
                    "update-ref",
                    "refs/remotes/origin/main",
                    unlanded_commit,
                ],
                check=True,
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

        provision_job = {
            "kind": "Job",
            "metadata": {
                "name": "wgcf-provision",
                "uid": "provision-uid",
                "labels": {
                    "app.kubernetes.io/name": "wgcf",
                    "app.kubernetes.io/component": "object-storage-maintenance",
                    "devint.profile": "governance-control-fabric",
                },
            },
            "spec": {
                "backoffLimit": 2,
                "template": {
                    "metadata": {
                        "labels": {
                            "app.kubernetes.io/name": "wgcf",
                            "app.kubernetes.io/component": "object-storage-maintenance",
                            "devint.profile": "governance-control-fabric",
                        },
                    },
                    "spec": {
                        "serviceAccountName": "wgcf-maintenance",
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "provision",
                                "image": "minio/mc:pinned",
                                "imagePullPolicy": "IfNotPresent",
                                "command": ["/bin/sh", "-ec"],
                                "args": ["provision"],
                                "env": [],
                                "volumeMounts": [],
                            },
                        ],
                        "volumes": [],
                    },
                },
            },
        }
        self.assertEqual(
            ISOLATION_MODULE.job_identity(
                [provision_job],
                provision_job,
                name="wgcf-provision",
            ),
            ("wgcf-provision", "provision-uid"),
        )
        replaced_job = json.loads(json.dumps(provision_job))
        replaced_job["spec"]["template"]["spec"]["containers"][0]["args"] = [
            "exfiltrate"
        ]
        with self.assertRaisesRegex(ValueError, "recorded provision template"):
            ISOLATION_MODULE.job_identity(
                [replaced_job],
                provision_job,
                name="wgcf-provision",
            )

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
