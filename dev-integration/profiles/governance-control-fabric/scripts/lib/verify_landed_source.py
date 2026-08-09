from __future__ import annotations

import subprocess
from pathlib import Path


def _run(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _reject_matching_url_rewrites(repo_root: Path, configured_url: str) -> None:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "config",
            "--get-regexp",
            r"^url\..*\.insteadof$",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 1:
        return
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "git URL rewrite inspection failed")
    for line in result.stdout.splitlines():
        _, rewrite_prefix = line.split(maxsplit=1)
        if configured_url.startswith(rewrite_prefix):
            raise SystemExit("approved origin remote is subject to a Git URL rewrite")


def require_landed_commit(repo_root: Path, repo_name: str, source_commit: str) -> None:
    expected_urls = {
        f"git@github.com:mfshaf7/{repo_name}.git",
        f"https://github.com/mfshaf7/{repo_name}.git",
        f"ssh://git@github.com/mfshaf7/{repo_name}.git",
    }
    configured_url = _run(repo_root, "config", "--get", "remote.origin.url")
    if configured_url not in expected_urls:
        raise SystemExit(f"{repo_name} does not use its approved origin remote")
    _reject_matching_url_rewrites(repo_root, configured_url)
    _run(repo_root, "fetch", "--quiet", "--no-tags", configured_url, "main")
    fetched_main = _run(repo_root, "rev-parse", "FETCH_HEAD")
    landed = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "merge-base",
            "--is-ancestor",
            source_commit,
            fetched_main,
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if landed.returncode != 0:
        raise SystemExit(f"{repo_name} revision is not landed on origin/main")
