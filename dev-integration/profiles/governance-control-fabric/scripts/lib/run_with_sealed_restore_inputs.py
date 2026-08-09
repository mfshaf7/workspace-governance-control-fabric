#!/usr/bin/env python3
"""Re-execute restore with immutable, descriptor-bound backup inputs."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import stat
import sys


REQUIRED_SEALS = (
    fcntl.F_SEAL_WRITE
    | fcntl.F_SEAL_SHRINK
    | fcntl.F_SEAL_GROW
    | fcntl.F_SEAL_SEAL
)


def _under_allowed_root(path: Path, allowed_roots: list[Path]) -> bool:
    return any(root == path or root in path.parents for root in allowed_roots)


def _sealed_copy(source: Path, *, name: str, allowed_roots: list[Path]) -> int:
    if not source.is_absolute():
        raise SystemExit("restore backup path must be absolute")
    resolved = source.resolve(strict=True)
    if not _under_allowed_root(resolved, allowed_roots):
        raise SystemExit(
            "restore backup must stay under the operator profile state or reset archive"
        )
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise SystemExit(f"restore input is not a regular file: {source}")
        sealed_fd = os.memfd_create(name, os.MFD_ALLOW_SEALING)
        try:
            while chunk := os.read(source_fd, 1024 * 1024):
                remaining = memoryview(chunk)
                while remaining:
                    remaining = remaining[os.write(sealed_fd, remaining) :]
            os.lseek(sealed_fd, 0, os.SEEK_SET)
            fcntl.fcntl(sealed_fd, fcntl.F_ADD_SEALS, REQUIRED_SEALS)
            if fcntl.fcntl(sealed_fd, fcntl.F_GET_SEALS) != REQUIRED_SEALS:
                raise SystemExit(f"restore input could not be sealed: {source}")
            os.set_inheritable(sealed_fd, True)
            return sealed_fd
        except BaseException:
            os.close(sealed_fd)
            raise
    finally:
        os.close(source_fd)


def main() -> int:
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: run_with_sealed_restore_inputs.py "
            "BACKUP_PATH STATE_ROOT ARCHIVE_ROOT RESTORE_SCRIPT"
        )
    backup_path = Path(sys.argv[1])
    allowed_roots = [Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()]
    restore_script = Path(sys.argv[4]).resolve(strict=True)
    archive_fd = _sealed_copy(
        backup_path,
        name="wgcf-restore-archive",
        allowed_roots=allowed_roots,
    )
    manifest_fd = _sealed_copy(
        Path(f"{backup_path}.manifest.json"),
        name="wgcf-restore-manifest",
        allowed_roots=allowed_roots,
    )
    environment = {
        **os.environ,
        "WGCF_RESTORE_INPUTS_SEALED": "1",
        "WGCF_RESTORE_ARCHIVE_FD": str(archive_fd),
        "WGCF_RESTORE_MANIFEST_FD": str(manifest_fd),
    }
    os.execve(restore_script, [str(restore_script)], environment)
    raise RuntimeError("failed to execute restore with sealed inputs")


if __name__ == "__main__":
    raise SystemExit(main())
