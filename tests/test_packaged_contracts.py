from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "smoke_packaged_contracts", REPO_ROOT / "scripts/smoke_packaged_contracts.py",
)
assert SPEC and SPEC.loader
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


def image_environment() -> dict[str, str]:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    base = dockerfile.split("FROM app-base AS worker", 1)[0]
    return dict(re.findall(r"^ENV (WGCF_\w+)=(\S+)$", base, re.MULTILINE))


class PackagedContractTests(TestCase):
    def test_both_images_require_installed_contract_smoke_as_runtime_user(self) -> None:
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        smoke = "USER wgcf\nRUN python -I /app/scripts/smoke_packaged_contracts.py\nUSER root"
        self.assertIn(smoke, dockerfile.split("FROM app-base AS worker", 1)[0])

    def test_image_binds_every_runtime_contract_root_before_both_targets(self) -> None:
        environment = image_environment()
        roots = set()
        for source in (REPO_ROOT / "packages/control_fabric_core/src/control_fabric_core").glob("*.py"):
            roots.update(re.findall(r"WGCF_[A-Z_]+_CONTRACT_ROOT", source.read_text(encoding="utf-8")))
        self.assertTrue(roots)
        self.assertFalse(roots - environment.keys(), f"unbound image contract roots: {roots - environment.keys()}")
        for key in roots:
            with self.subTest(root=key):
                value = environment[key]
                self.assertTrue(value.startswith("/app/contracts/"))
                self.assertTrue((REPO_ROOT / value.removeprefix("/app/") / "manifest.json").is_file())

    def test_normal_loaders_accept_the_image_declared_bundles(self) -> None:
        environment = {
            key: str(REPO_ROOT / value.removeprefix("/app/"))
            for key, value in image_environment().items()
        }
        with patch.dict(os.environ, environment):
            self.assertEqual(SMOKE.verify_contracts(require_installed=False), 13)

    def test_missing_custody_bundle_fails_instead_of_health_only_success(self) -> None:
        environment = {
            key: str(REPO_ROOT / value.removeprefix("/app/"))
            for key, value in image_environment().items()
        }
        with TemporaryDirectory() as missing:
            environment["WGCF_REPOSITORY_CUSTODY_CONTRACT_ROOT"] = missing
            with patch.dict(os.environ, environment):
                with self.assertRaisesRegex(ValueError, "custody contract manifest is unavailable"):
                    SMOKE.verify_contracts(require_installed=False)
