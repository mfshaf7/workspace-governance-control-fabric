from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeliveryArtEvidenceProfileTests(unittest.TestCase):
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
