from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_ROOT = REPO_ROOT / "dev-integration/profiles/governance-control-fabric"
SCRIPTS_ROOT = PROFILE_ROOT / "scripts"


class DevIntegrationProfileTests(TestCase):
    def test_profile_declares_k3s_api_runtime(self) -> None:
        profile = yaml.safe_load((PROFILE_ROOT / "profile.yaml").read_text(encoding="utf-8"))

        self.assertEqual(profile["profile_id"], "governance-control-fabric")
        self.assertEqual(profile["runtime"]["platform"], "local-k3s")
        self.assertEqual(profile["runtime"]["state_model"], "persistent")
        self.assertIn("workspace-governance-control-fabric-postgresql", profile["runtime"]["components"])
        self.assertEqual(profile["testing"]["smoke"]["mutation_mode"], "read-only")
        self.assertIn("API readiness", profile["stage_handoff"]["required_checks"])
        self.assertIn("database migration", profile["stage_handoff"]["required_checks"])
        self.assertIn(
            "receipt and ledger metadata read",
            profile["stage_handoff"]["required_checks"],
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
                "DEVINT_WORKSPACE_ROOT": str(REPO_ROOT.parent),
            }

            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"source {SCRIPTS_ROOT / 'common.sh'}; render_runtime_manifest; write_access_file",
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

            self.assertIn("kind: Deployment", manifest)
            self.assertIn("kind: StatefulSet", manifest)
            self.assertIn("kind: Service", manifest)
            self.assertIn("workspace-governance-control-fabric-postgresql", manifest)
            self.assertIn("WGCF_DATABASE_URL", manifest)
            self.assertIn("value: /var/lib/postgresql/data/pgdata", manifest)
            self.assertIn("image: ghcr.io/mfshaf7/workspace-governance-control-fabric:sha-test", manifest)
            self.assertIn("runAsNonRoot: true", manifest)
            self.assertIn("allowPrivilegeEscalation: false", manifest)
            self.assertIn("service: workspace-governance-control-fabric-api", access)
            self.assertIn("postgres_service: workspace-governance-control-fabric-postgresql", access)
