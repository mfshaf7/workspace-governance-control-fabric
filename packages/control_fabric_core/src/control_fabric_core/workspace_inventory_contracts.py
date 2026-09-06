"""Pinned contracts and committed authority snapshots for active inventory."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .workspace_intake_contracts import (
    IntakeRequestError,
    IntakeUnavailable,
    artifact_digest as _artifact_digest,
    canonical_bytes as _canonical_bytes,
    digest as _digest,
    parse_json as _parse_json,
    yaml_record as _yaml_record,
)


CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "contracts" / "workspace-active-inventory"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
COLLECTIONS = {"repo": "repos", "product": "products", "component": "components"}
INVENTORY_PATHS = {
    "repo": "contracts/repos.yaml",
    "product": "contracts/products.yaml",
    "component": "contracts/components.yaml",
}
HISTORY_PATH = "contracts/workspace-inventory-history.yaml"
SNAPSHOT_PATHS = ("contracts/intake-register.yaml", *INVENTORY_PATHS.values(), HISTORY_PATH)


class InventoryRequestError(ValueError):
    """Invalid transport or artifact integrity, not a readiness decision."""


class InventoryUnavailable(RuntimeError):
    """Contracts, authority, or immutable evidence cannot be trusted."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return _canonical_bytes(value)
    except IntakeRequestError as exc:
        raise InventoryRequestError(str(exc)) from exc


def parse_json(raw: bytes) -> Any:
    try:
        return _parse_json(raw)
    except (IntakeRequestError, UnicodeDecodeError) as exc:
        raise InventoryRequestError(str(exc)) from exc


def digest(value: Any) -> str:
    try:
        return _digest(value)
    except IntakeRequestError as exc:
        raise InventoryRequestError(str(exc)) from exc


def artifact_digest(value: dict[str, Any], field: str) -> str:
    try:
        return _artifact_digest(value, field)
    except IntakeRequestError as exc:
        raise InventoryRequestError(str(exc)) from exc


def yaml_record(raw: bytes) -> dict[str, Any]:
    try:
        return _yaml_record(raw)
    except (IntakeUnavailable, IntakeRequestError) as exc:
        raise InventoryUnavailable(str(exc)) from exc


@dataclass(frozen=True)
class InventoryContracts:
    manifest: dict[str, Any]
    files: dict[str, bytes]
    validators: dict[str, Draft202012Validator]

    @classmethod
    def load(cls, root: Path | None = None) -> InventoryContracts:
        root = Path(
            root
            or os.environ.get("WGCF_WORKSPACE_ACTIVE_INVENTORY_CONTRACT_ROOT")
            or CONTRACT_ROOT
        )
        try:
            manifest = parse_json((root / "manifest.json").read_bytes())
            if (
                manifest["contract_id"] != "wgcf.workspace-active-inventory-readiness.v1"
                or manifest["authority_repo"] != "workspace-governance"
                or not COMMIT_PATTERN.fullmatch(manifest["authority_commit"])
            ):
                raise ValueError("invalid active inventory bundle manifest")
            expected = {
                "contracts/workspace-active-inventory.yaml",
                "contracts/workspace-inventory-lifecycle.yaml",
                "contracts/schemas/workspace-inventory-promotion-request.schema.json",
                "contracts/schemas/workspace-inventory-promotion-readiness.schema.json",
                "contracts/schemas/workspace-inventory-lifecycle-request.schema.json",
                "contracts/schemas/workspace-inventory-lifecycle-readiness.schema.json",
                "contracts/schemas/workspace-inventory-lifecycle.schema.json",
                "contracts/schemas/workspace-inventory-history.schema.json",
                "contracts/schemas/intake-register.schema.json",
                "contracts/schemas/repos.schema.json",
                "contracts/schemas/products.schema.json",
                "contracts/schemas/components.schema.json",
            }
            if set(manifest["files"]) != expected:
                raise ValueError("incomplete active inventory contract bundle")
            if set(manifest["transport_schemas"]) != {
                "evaluation.schema.json",
                "lifecycle-evaluation.schema.json",
            }:
                raise ValueError("incomplete active inventory transport contract")

            files: dict[str, bytes] = {}
            validators: dict[str, Draft202012Validator] = {}
            for path, expected_digest in manifest["files"].items():
                raw = (root / Path(path).name).read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected_digest:
                    raise ValueError("active inventory bundle digest mismatch")
                files[path] = raw
                if path.endswith(".json"):
                    schema = parse_json(raw)
                    Draft202012Validator.check_schema(schema)
                    validators[Path(path).name] = Draft202012Validator(
                        schema,
                        format_checker=FormatChecker(),
                    )
            for name, expected_digest in manifest["transport_schemas"].items():
                raw = (root / name).read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected_digest:
                    raise ValueError("active inventory transport digest mismatch")
                schema = parse_json(raw)
                Draft202012Validator.check_schema(schema)
                validators[name] = Draft202012Validator(
                    schema,
                    format_checker=FormatChecker(),
                )
            return cls(manifest=manifest, files=files, validators=validators)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            SchemaError,
            InventoryRequestError,
        ) as exc:
            raise InventoryUnavailable(
                "active inventory contract bundle is unavailable or invalid"
            ) from exc

    def validate(self, name: str, record: Any) -> None:
        try:
            validator = self.validators[name]
        except KeyError as exc:
            raise InventoryUnavailable(f"unknown active inventory schema: {name}") from exc
        errors = sorted(validator.iter_errors(record), key=lambda error: list(error.absolute_path))
        if errors:
            error = errors[0]
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            raise InventoryRequestError(f"{name}: {path}: {error.message}")
        canonical_bytes(record)


@dataclass(frozen=True)
class InventorySnapshot:
    revision: str
    records: dict[str, dict[str, Any]]
    file_digests: dict[str, str]


class InventoryAuthority:
    """Read active inventory truth from one configured committed Git ref."""

    def __init__(
        self,
        repo_root: Path,
        contracts: InventoryContracts,
        *,
        trusted_ref: str = "refs/remotes/origin/main",
    ) -> None:
        self.repo_root = repo_root
        self.contracts = contracts
        self.trusted_ref = trusted_ref

    def _git(self, *args: str) -> bytes:
        try:
            return subprocess.run(
                ["git", "-C", str(self.repo_root), *args],
                check=True,
                capture_output=True,
                timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise InventoryUnavailable("committed active inventory authority is unavailable") from exc

    def snapshot(self) -> InventorySnapshot:
        revision = self._git(
            "rev-parse", "--verify", f"{self.trusted_ref}^{{commit}}"
        ).decode().strip()
        if not COMMIT_PATTERN.fullmatch(revision):
            raise InventoryUnavailable("active inventory authority revision is invalid")
        self._git(
            "merge-base",
            "--is-ancestor",
            self.contracts.manifest["authority_commit"],
            revision,
        )

        raw_files: dict[str, bytes] = {}
        for path, expected in self.contracts.files.items():
            raw = self._git("show", f"{revision}:{path}")
            if raw != expected:
                raise InventoryUnavailable(
                    "authority contract changed; update the reviewed active inventory bundle"
                )
            raw_files[path] = raw

        records: dict[str, dict[str, Any]] = {}
        for path in SNAPSHOT_PATHS:
            raw = self._git("show", f"{revision}:{path}")
            if len(raw) > 4 * 1024 * 1024:
                raise InventoryUnavailable("active inventory authority exceeds the snapshot limit")
            raw_files[path] = raw
            records[Path(path).stem] = yaml_record(raw)

        try:
            self.contracts.validate("intake-register.schema.json", records["intake-register"])
            for kind, path in INVENTORY_PATHS.items():
                self.contracts.validate(
                    f"{COLLECTIONS[kind]}.schema.json",
                    records[Path(path).stem],
                )
            self.contracts.validate(
                "workspace-inventory-history.schema.json",
                records[Path(HISTORY_PATH).stem],
            )
            validate_history(records[Path(HISTORY_PATH).stem])
        except InventoryRequestError as exc:
            raise InventoryUnavailable("canonical active inventory authority is invalid") from exc

        return InventorySnapshot(
            revision=revision,
            records=records,
            file_digests={
                path: "sha256:" + hashlib.sha256(raw).hexdigest()
                for path, raw in raw_files.items()
            },
        )


def validate_history(history: dict[str, Any]) -> None:
    """Validate append-only event identity, digest, order, and chain continuity."""

    seen_ids: set[str] = set()
    seen_idempotency: set[str] = set()
    seen_requests: set[str] = set()
    by_target: dict[str, list[dict[str, Any]]] = {}
    for event in history["events"]:
        event_id = event["event_id"]
        idempotency_key = event["idempotency_key"]
        if event_id in seen_ids:
            raise InventoryUnavailable("active inventory history reuses an event identity")
        if idempotency_key in seen_idempotency:
            raise InventoryUnavailable("active inventory history reuses an idempotency key")
        request_id = event["request_ref"]["id"]
        if request_id in seen_requests:
            raise InventoryUnavailable("active inventory history reuses a request identity")
        seen_ids.add(event_id)
        seen_idempotency.add(idempotency_key)
        seen_requests.add(request_id)
        projection = dict(event)
        projection.pop("event_digest")
        if event["event_digest"] != digest(projection):
            raise InventoryUnavailable("active inventory history event digest is invalid")
        target = event["target"]
        if target["record_id"] != f"{target['kind']}:{target['name']}":
            raise InventoryUnavailable("active inventory history target identity is invalid")
        by_target.setdefault(target["record_id"], []).append(event)

    for events in by_target.values():
        for index, event in enumerate(events, start=1):
            if event["sequence"] != index:
                raise InventoryUnavailable("active inventory history sequence is not contiguous")
            previous_ref = None
            if index > 1:
                previous = events[index - 2]
                previous_ref = {
                    "id": previous["event_id"],
                    "digest": previous["event_digest"],
                }
                if event["before"] != previous["after"]:
                    raise InventoryUnavailable("active inventory history chain is discontinuous")
            if event["previous_event_ref"] != previous_ref:
                raise InventoryUnavailable("active inventory history predecessor is invalid")
