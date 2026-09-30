from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_runner_module():
    path = ROOT / "scripts/run_delivery_art_evidence.py"
    spec = importlib.util.spec_from_file_location("delivery_art_evidence_runner", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DeliveryArtEvidenceProfileTests(unittest.TestCase):
    def test_owner_test_runner_removes_ambient_control_plane_environment(self) -> None:
        runner = load_runner_module()
        original = runner.os.environ
        runner.os.environ = {
            "PATH": "/usr/bin",
            "HTTPS_PROXY": "http://proxy.example.invalid",
            "DEVINT_SESSION_FILE": "/tmp/session.yaml",
            "GIT_ASKPASS": "/tmp/askpass",
            "OOS_DELIVERY_SOURCE_EXECUTOR_SECRET": "secret",
            "WGCF_RUNTIME_PROFILE": "dev-integration",
        }
        try:
            environment = runner.isolated_environment(ROOT)
        finally:
            runner.os.environ = original

        self.assertEqual("/usr/bin", environment["PATH"])
        self.assertEqual(
            "http://proxy.example.invalid",
            environment["HTTPS_PROXY"],
        )
        self.assertNotIn("DEVINT_SESSION_FILE", environment)
        self.assertNotIn("GIT_ASKPASS", environment)
        self.assertNotIn("OOS_DELIVERY_SOURCE_EXECUTOR_SECRET", environment)
        self.assertNotIn("WGCF_RUNTIME_PROFILE", environment)
        self.assertIn("packages/control_fabric_core/src", environment["PYTHONPATH"])

    def test_profile_uses_isolated_owner_test_runner(self) -> None:
        profile = json.loads(
            (ROOT / "contracts/delivery-art-work-session/evidence-profile.json").read_text()
        )
        test_command = next(
            command for command in profile["commands"] if command["kind"] == "tests"
        )
        self.assertEqual("python3", test_command["executable"])
        self.assertEqual(
            ["scripts/run_delivery_art_evidence.py", "--repo-root", "."],
            test_command["args"],
        )
        self.assertEqual("filesystem", test_command["fidelity"])
        self.assertEqual("matching-fidelity", test_command["conformance_binding"])

        validation_command = next(
            command for command in profile["commands"]
            if command["id"] == "source-diff-validation"
        )
        self.assertEqual("real-git", validation_command["fidelity"])
        self.assertEqual(
            "matching-fidelity",
            validation_command["conformance_binding"],
        )

    def test_owner_test_runner_has_a_bounded_cli(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/run_delivery_art_evidence.py", "--help"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("--repo-root", completed.stdout)


if __name__ == "__main__":
    unittest.main()
