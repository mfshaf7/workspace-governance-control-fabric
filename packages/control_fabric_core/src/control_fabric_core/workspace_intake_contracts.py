"""Pinned intake vocabulary and read-only committed authority snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
import yaml

CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "contracts" / "workspace-intake"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
COLLECTIONS = {"repo": "repos", "product": "products", "component": "components"}
SNAPSHOT_PATHS = (
    "contracts/intake-register.yaml",
    "contracts/repos.yaml",
    "contracts/products.yaml",
    "contracts/components.yaml",
    "contracts/governed-intake-assist.yaml",
)


class IntakeRequestError(ValueError):
    """Invalid transport or artifact integrity; not a readiness decision."""


class IntakeUnavailable(RuntimeError):
    """Authority, contracts, or immutable evidence cannot be trusted."""


def canonical_bytes(value: Any) -> bytes:
    # Intake v2 deliberately uses the workspace algorithm, not RFC8785.
    def check(node: Any) -> None:
        if isinstance(node, float):
            raise IntakeRequestError("floating point is not allowed in intake artifacts")
        if isinstance(node, dict):
            for key, child in node.items():
                if not isinstance(key, str):
                    raise IntakeRequestError("intake object keys must be strings")
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)
    check(value)
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def parse_json(raw: bytes) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise IntakeRequestError("duplicate JSON key")
            result[key] = value
        return result
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    canonical_bytes(value)
    return value


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def artifact_digest(value: dict[str, Any], field: str) -> str:
    return digest({key: child for key, child in value.items() if key != field})


class UniqueLoader(yaml.SafeLoader):
    """Reject ambiguous YAML authority mappings."""


def _mapping(loader: UniqueLoader, node: Any) -> dict[str, Any]:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise IntakeUnavailable("authority contains a duplicate or non-string key")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def yaml_record(raw: bytes) -> dict[str, Any]:
    try:
        value = yaml.load(raw, Loader=UniqueLoader)
        if not isinstance(value, dict):
            raise IntakeUnavailable("authority document must be an object")
        canonical_bytes(value)
        return value
    except (ValueError, TypeError, yaml.YAMLError) as exc:
        raise IntakeUnavailable("authority document is invalid") from exc


@dataclass(frozen=True)
class IntakeContracts:
    manifest: dict[str, Any]
    files: dict[str, bytes]
    validators: dict[str, Draft202012Validator]
    policy: dict[str, Any]

    @classmethod
    def load(cls, root: Path | None = None) -> IntakeContracts:
        root = Path(root or os.environ.get("WGCF_WORKSPACE_INTAKE_CONTRACT_ROOT") or CONTRACT_ROOT)
        try:
            manifest = parse_json((root / "manifest.json").read_bytes())
            if (
                manifest["contract_id"] != "wgcf.workspace-intake-readiness.v1"
                or not COMMIT_PATTERN.fullmatch(manifest["authority_commit"])
                or manifest["authority_repo"] != "workspace-governance"
            ):
                raise ValueError("invalid intake bundle manifest")
            expected = {
                "contracts/workspace-intake.yaml", "contracts/intake-policy.yaml",
                "contracts/schemas/workspace-intake-request.schema.json",
                "contracts/schemas/workspace-intake-decision.schema.json",
                "contracts/schemas/intake-register.schema.json",
            }
            if set(manifest["files"]) != expected:
                raise ValueError("incomplete intake bundle")
            files = {}
            validators = {}
            for path, expected_digest in manifest["files"].items():
                raw = (root / Path(path).name).read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected_digest:
                    raise ValueError("intake bundle digest mismatch")
                files[path] = raw
                if path.endswith(".json"):
                    schema = parse_json(raw)
                    Draft202012Validator.check_schema(schema)
                    validators[Path(path).name] = Draft202012Validator(
                        schema, format_checker=FormatChecker(),
                    )
            if set(manifest["transport_schemas"]) != {"evaluation.schema.json", "readiness.schema.json"}:
                raise ValueError("incomplete intake transport contract")
            for name, expected_digest in manifest["transport_schemas"].items():
                raw = (root / name).read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected_digest:
                    raise ValueError("intake transport contract digest mismatch")
                schema = parse_json(raw)
                Draft202012Validator.check_schema(schema)
                validators[name] = Draft202012Validator(schema, format_checker=FormatChecker())
            return cls(manifest, files, validators, yaml_record(files["contracts/intake-policy.yaml"]))
        except (OSError, ValueError, KeyError, TypeError, SchemaError) as exc:
            raise IntakeUnavailable("intake contract bundle is unavailable or invalid") from exc

    def validate(self, name: str, record: Any) -> None:
        errors = list(self.validators[name].iter_errors(record))
        if errors:
            error = errors[0]
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            raise IntakeRequestError(f"{name}: {path}: {error.message}")
        canonical_bytes(record)


@dataclass(frozen=True)
class IntakeSnapshot:
    revision: str
    records: dict[str, Any]
    file_digests: dict[str, str]


class IntakeAuthority:
    """Only configured authority refs may supply policy and current inventory."""

    def __init__(
        self, repo_root: Path, contracts: IntakeContracts, *, trusted_ref: str = "refs/remotes/origin/main",
    ) -> None:
        self.repo_root = repo_root
        self.contracts = contracts
        self.trusted_ref = trusted_ref

    def _git(self, *args: str) -> bytes:
        try:
            return subprocess.run(
                ["git", "-C", str(self.repo_root), *args],
                check=True, capture_output=True, timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise IntakeUnavailable("committed intake authority is unavailable") from exc

    def snapshot(self) -> IntakeSnapshot:
        revision = self._git("rev-parse", "--verify", f"{self.trusted_ref}^{{commit}}").decode().strip()
        if not COMMIT_PATTERN.fullmatch(revision):
            raise IntakeUnavailable("authority revision is invalid")
        self._git("merge-base", "--is-ancestor", self.contracts.manifest["authority_commit"], revision)
        files = {}
        for path, expected in self.contracts.files.items():
            raw = self._git("show", f"{revision}:{path}")
            if raw != expected:
                raise IntakeUnavailable("authority contract changed; update the reviewed intake bundle")
            files[path] = raw
        records = {}
        for path in SNAPSHOT_PATHS:
            raw = self._git("show", f"{revision}:{path}")
            if len(raw) > 4 * 1024 * 1024:
                raise IntakeUnavailable("authority document exceeds the snapshot limit")
            files[path] = raw
            records[Path(path).stem] = yaml_record(raw)
        for name in COLLECTIONS.values():
            if not isinstance(records[name].get(name), dict):
                raise IntakeUnavailable("canonical inventory collection is invalid")
        if not isinstance(records["repos"].get("retired_repos", {}), dict):
            raise IntakeUnavailable("retired repository inventory is invalid")
        try:
            self.contracts.validate("intake-register.schema.json", records["intake-register"])
        except IntakeRequestError as exc:
            raise IntakeUnavailable("canonical intake register is invalid") from exc
        return IntakeSnapshot(
            revision, records,
            {path: "sha256:" + hashlib.sha256(raw).hexdigest() for path, raw in files.items()},
        )
