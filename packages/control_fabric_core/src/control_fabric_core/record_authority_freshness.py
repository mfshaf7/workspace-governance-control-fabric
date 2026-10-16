"""Read-only admission of Platform-maintained canonical record snapshots.

This is a freshness check, not source refresh or policy authority. Platform
creates the private clone and accepts its record-only advancement. Existing
owner readers still enforce their own pinned schemas, content and history.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Mapping


def require_record_authority_freshness(
    repo_root: Path,
    owner_repo: str,
    revision: str | None = None,
    *,
    trusted_ref: str = "refs/remotes/origin/main",
    unavailable: type[Exception] = RuntimeError,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> str | None:
    selected_env = os.environ if env is None else env
    if selected_env.get("WGCF_RECORD_AUTHORITY_FRESHNESS_REQUIRED") != "true":
        return
    try:
        checkpoint = repo_root / ".git" / "console-record-authority.json"
        info = checkpoint.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2048:
            raise ValueError("invalid checkpoint file")
        value = json.loads(checkpoint.read_bytes())
        observed = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(value["valid_until"].replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        if (
            type(value.get("schema_version")) is not int or value.get("schema_version") != 1
            or value.get("owner_repo") != owner_repo
            or owner_repo not in {"workspace-governance", "workspace-prototype-studio"}
            or trusted_ref != "refs/remotes/origin/main"
            or value.get("trusted_ref") != trusted_ref
            or not re.fullmatch(r"[0-9a-f]{40}", value.get("reviewed_revision", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", value.get("record_revision", ""))
            or revision is not None and value["record_revision"] != revision
            or observed.tzinfo is None or expires.tzinfo is None
            or (observed - current).total_seconds() > 30
            or expires <= current or expires <= observed
            or (expires - observed).total_seconds() > 600
        ):
            raise ValueError("stale or mismatched checkpoint")
        git_env = {key: val for key, val in selected_env.items() if not key.startswith("GIT_")}
        git_env.update({
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_GRAFT_FILE": os.devnull, "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
        })
        def git(*args: str) -> str:
            return subprocess.run([
                "git", "-c", f"safe.directory={repo_root.resolve()}",
                "-c", f"core.hooksPath={os.devnull}", "-C", str(repo_root), *args,
            ], check=True, capture_output=True, timeout=10, env=git_env).stdout.decode().strip()
        if git("rev-parse", "--verify", f"{trusted_ref}^{{commit}}") != value["record_revision"]:
            raise ValueError("checkpoint is not the current trusted ref")
        git("merge-base", "--is-ancestor", value["reviewed_revision"], value["record_revision"])
        if revision is None and (
            git("rev-parse", "HEAD") != value["record_revision"]
            or git("status", "--porcelain", "--untracked-files=normal")
        ):
            raise ValueError("tree reader does not match the fresh record checkpoint")
        return value["record_revision"]
    except (OSError, ValueError, TypeError, AttributeError, KeyError, subprocess.SubprocessError):
        raise unavailable(
            "deployment-owned canonical record snapshot is missing, stale or inconsistent; "
            "restore Platform record refresh before continuing"
        ) from None
