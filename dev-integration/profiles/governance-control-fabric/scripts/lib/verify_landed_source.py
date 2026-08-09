from __future__ import annotations

import os
import pwd
import subprocess
import tempfile
from pathlib import Path

GIT_EXECUTABLE = "/usr/bin/git"


def _git_environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("GIT_"):
            env.pop(key)
    env.update(
        {
            "GIT_SSH_COMMAND": (
                "/usr/bin/ssh -F /dev/null -oCanonicalizeHostname=no "
                "-oClearAllForwardings=yes -oPermitLocalCommand=no "
                "-oProxyCommand=none -oProxyJump=none -oStrictHostKeyChecking=yes"
            ),
            "GIT_SSH_VARIANT": "ssh",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_GRAFT_FILE": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "HOME": pwd.getpwuid(os.getuid()).pw_dir,
            "LC_ALL": "C",
        }
    )
    return env


def _run(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        [GIT_EXECUTABLE, "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        env=_git_environment(),
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _reject_matching_url_rewrites(repo_root: Path, configured_url: str) -> None:
    result = subprocess.run(
        [
            GIT_EXECUTABLE,
            "-C",
            str(repo_root),
            "config",
            "--get-regexp",
            r"^url\..*\.insteadof$",
        ],
        check=False,
        capture_output=True,
        env=_git_environment(),
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


def read_landed_source_file(
    repo_root: Path,
    repo_name: str,
    source_commit: str,
    source_path: str,
) -> bytes:
    expected_urls = {
        f"git@github.com:mfshaf7/{repo_name}.git",
        f"ssh://git@github.com/mfshaf7/{repo_name}.git",
    }
    configured_url = _run(repo_root, "config", "--get", "remote.origin.url")
    if configured_url not in expected_urls:
        raise SystemExit(f"{repo_name} does not use its approved origin remote")
    _reject_matching_url_rewrites(repo_root, configured_url)
    with tempfile.TemporaryDirectory(prefix="wgcf-landed-source-") as temp_dir:
        clean_git_dir = Path(temp_dir) / "authority.git"
        initialized = subprocess.run(
            [GIT_EXECUTABLE, "init", "--bare", "--quiet", str(clean_git_dir)],
            check=False,
            capture_output=True,
            env=_git_environment(),
            text=True,
        )
        if initialized.returncode != 0:
            raise SystemExit(initialized.stderr.strip() or "clean Git authority initialization failed")

        clean_args = ["--git-dir", str(clean_git_dir)]
        _run(
            repo_root,
            *clean_args,
            "fetch",
            "--quiet",
            "--no-tags",
            configured_url,
            "main",
        )
        fetched_main = _run(repo_root, *clean_args, "rev-parse", "FETCH_HEAD")
        landed = subprocess.run(
            [
                GIT_EXECUTABLE,
                "-C",
                str(repo_root),
                *clean_args,
                "merge-base",
                "--is-ancestor",
                source_commit,
                fetched_main,
            ],
            check=False,
            env=_git_environment(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if landed.returncode != 0:
            raise SystemExit(f"{repo_name} revision is not landed on origin/main")

        result = subprocess.run(
            [
                GIT_EXECUTABLE,
                "-C",
                str(repo_root),
                *clean_args,
                "show",
                f"{source_commit}:{source_path}",
            ],
            check=False,
            env=_git_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise SystemExit(f"Pinned source is unavailable: {source_commit}:{source_path}")
        return result.stdout
