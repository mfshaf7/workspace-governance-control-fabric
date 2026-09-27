#!/usr/bin/env python3
"""Run WGCF Delivery ART tests in an isolated dependency environment."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import venv
from pathlib import Path


SOURCE_PATHS = (
    "packages/control_fabric_core/src",
    "apps/api/src",
    "apps/cli/src",
    "apps/worker/src",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser.parse_args()


def main() -> int:
    repo_root = parse_args().repo_root.resolve()
    with tempfile.TemporaryDirectory(prefix="wgcf-delivery-art-evidence-") as temp:
        environment_root = Path(temp) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment_root)
        python = environment_root / "bin" / "python"
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "-e",
                f"{repo_root}[test]",
            ],
            check=True,
            cwd=repo_root,
        )
        environment = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                str(repo_root / source_path) for source_path in SOURCE_PATHS
            ),
        }
        subprocess.run(
            [str(python), "scripts/validate_project.py", "--repo-root", "."],
            check=True,
            cwd=repo_root,
            env=environment,
        )
        subprocess.run(
            [str(python), "-m", "unittest", "discover", "-s", "tests"],
            check=True,
            cwd=repo_root,
            env=environment,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
