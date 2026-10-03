#!/usr/bin/env python3
"""Validate governed WGCF change records without rewriting legacy records."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

import yaml


CHANGE_RECORD_RE = re.compile(r"\d{4}-\d{2}-\d{2}-[a-z0-9-]+\.md$")
REVIEW_AREAS = {"identity", "secrets", "delivery", "runtime", "ai"}
FINDING_RE = re.compile(r"^F-\d{3}$")
RISK_RE = re.compile(r"^R-\d{3}$")
WORKSTREAM_RE = re.compile(r"^WS-\d{3}$")
LOCAL_LINK_RE = re.compile(r"\]\(/home/mfshaf7/projects/")
REQUIRED_HEADINGS = {
    "## Summary",
    "## Classification",
    "## Ownership",
    "## Root Cause",
    "## Source Changes",
    "## Artifact And Deployment Evidence",
    "## Live Verification",
    "## Follow-Up",
}


def parse_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        raise ValueError("front matter is not terminated")
    metadata = yaml.safe_load(parts[0][4:]) or {}
    if not isinstance(metadata, dict):
        raise ValueError("front matter must be a mapping")
    return metadata, parts[1]


def validate_security_evidence(path: Path, value: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return [f"{path}: security_evidence front matter must be a mapping"]
    review_areas = value.get("review_areas")
    if (
        not isinstance(review_areas, list)
        or not review_areas
        or any(
            not isinstance(item, str) or item not in REVIEW_AREAS
            for item in review_areas
        )
    ):
        errors.append(
            f"{path}: security_evidence.review_areas must be a non-empty list "
            "of valid review areas",
        )
    for field, pattern, label in (
        ("findings", FINDING_RE, "F-###"),
        ("risks", RISK_RE, "R-###"),
        ("workstreams", WORKSTREAM_RE, "WS-###"),
    ):
        entries = value.get(field) or []
        if not isinstance(entries, list) or any(
            not isinstance(item, str) or not pattern.fullmatch(item)
            for item in entries
        ):
            errors.append(
                f"{path}: security_evidence.{field} must be a list of {label} ids",
            )
    if not value.get("workstreams"):
        errors.append(
            f"{path}: security_evidence.workstreams must contain at least one WS-### id",
        )
    return errors


def validate_change_records(repo_root: Path) -> list[str]:
    records_dir = repo_root / "docs" / "records" / "change-records"
    errors: list[str] = []
    if not records_dir.is_dir():
        return [f"{records_dir}: missing change-record directory"]
    for path in sorted(records_dir.glob("*.md")):
        if not CHANGE_RECORD_RE.fullmatch(path.name):
            errors.append(f"{path}: invalid change-record filename")
            continue
        try:
            metadata, body = parse_front_matter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, yaml.YAMLError) as error:
            errors.append(f"{path}: {error}")
            continue
        if not metadata:
            continue
        missing = sorted(heading for heading in REQUIRED_HEADINGS if heading not in body)
        if missing:
            errors.append(f"{path}: missing governed headings: {', '.join(missing)}")
        if LOCAL_LINK_RE.search(body):
            errors.append(
                f"{path}: git-tracked records must not link to /home/mfshaf7/projects",
            )
        if "security_evidence" in metadata:
            errors.extend(
                validate_security_evidence(path, metadata["security_evidence"]),
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate WGCF governance docs.")
    parser.add_argument(
        "--repo-root",
        default=Path(__file__).resolve().parents[1],
        type=Path,
    )
    args = parser.parse_args()
    errors = validate_change_records(args.repo_root.resolve())
    if errors:
        raise SystemExit("\n".join(errors))
    print("workspace-governance-control-fabric governance docs valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
