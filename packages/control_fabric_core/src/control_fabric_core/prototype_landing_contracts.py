"""Pinned contracts and committed source authority for Prototype Landing readiness."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
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


CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "contracts" / "prototype-landing"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
AUTHORITY_FILES = {
    "prototype-landing.yaml",
    "prototype-landing.schema.json",
    "prototype-landing-entry-packet.schema.json",
    "prototype-landing-request.schema.json",
    "prototype-landing-plan.schema.json",
    "prototype-landing-readiness.schema.json",
}
SOURCE_FILES = {"schemas/prototype-registry.schema.json"}
REGISTRY_PATH = "prototypes.yaml"
LANDING_RECORD_ROOT = "records/prototype-landings"


class PrototypeLandingRequestError(ValueError):
    """Invalid transport or artifact integrity, not a readiness decision."""


class PrototypeLandingUnavailable(RuntimeError):
    """Pinned contracts, source authority, or durable evidence are unavailable."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return _canonical_bytes(value)
    except IntakeRequestError as exc:
        raise PrototypeLandingRequestError(str(exc)) from exc


def parse_json(raw: bytes) -> Any:
    try:
        return _parse_json(raw)
    except (IntakeRequestError, UnicodeDecodeError) as exc:
        raise PrototypeLandingRequestError(str(exc)) from exc


def digest(value: Any) -> str:
    try:
        return _digest(value)
    except IntakeRequestError as exc:
        raise PrototypeLandingRequestError(str(exc)) from exc


def artifact_digest(value: dict[str, Any], field: str) -> str:
    try:
        return _artifact_digest(value, field)
    except IntakeRequestError as exc:
        raise PrototypeLandingRequestError(str(exc)) from exc


def _yaml(raw: bytes) -> dict[str, Any]:
    try:
        return _yaml_record(raw)
    except (IntakeRequestError, IntakeUnavailable, TypeError, ValueError) as exc:
        raise PrototypeLandingUnavailable("Prototype Studio registry is malformed") from exc


@dataclass(frozen=True)
class PrototypeLandingContracts:
    manifest: dict[str, Any]
    files: dict[str, bytes]
    validators: dict[str, Draft202012Validator]

    @classmethod
    def load(cls, root: Path | None = None) -> "PrototypeLandingContracts":
        root = Path(root or os.environ.get("WGCF_PROTOTYPE_LANDING_CONTRACT_ROOT") or CONTRACT_ROOT)
        try:
            manifest = parse_json((root / "manifest.json").read_bytes())
            activation_review = manifest["activation_review"]
            if (
                manifest["contract_id"] != "wgcf.prototype-landing-readiness.v1"
                or manifest["authority_repo"] != "workspace-governance"
                or not COMMIT_PATTERN.fullmatch(manifest["authority_commit"])
                or manifest["security_review"]["repo"] != "security-architecture"
                or not COMMIT_PATTERN.fullmatch(manifest["security_review"]["commit"])
                or activation_review["repo"] != "security-architecture"
                or not COMMIT_PATTERN.fullmatch(activation_review["commit"])
                or not activation_review["path"].startswith("docs/reviews/components/")
                or not activation_review["path"].endswith(".md")
                or not re.fullmatch(r"[0-9a-f]{64}", activation_review["content_sha256"])
                or activation_review["decision"] != "approved-with-findings"
                or manifest["source_authority"]["repo"] != "workspace-prototype-studio"
                or not COMMIT_PATTERN.fullmatch(manifest["source_authority"]["minimum_commit"])
                or manifest["runtime_activation"] is not True
                or manifest["security_review"]["decision"] != "approved-with-findings"
            ):
                raise ValueError("invalid Prototype Landing bundle manifest")
            if set(manifest["files"]) != AUTHORITY_FILES:
                raise ValueError("incomplete Prototype Landing authority bundle")
            if set(manifest["transport_schemas"]) != {"evaluation.schema.json"}:
                raise ValueError("incomplete Prototype Landing transport bundle")
            if set(manifest["source_authority"]["files"]) != SOURCE_FILES:
                raise ValueError("incomplete Prototype Studio source contract bundle")

            files: dict[str, bytes] = {}
            validators: dict[str, Draft202012Validator] = {}
            for name, expected in manifest["files"].items():
                raw = (root / name).read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError(f"Prototype Landing contract digest differs for {name}")
                files[name] = raw
                if name.endswith(".json"):
                    schema = parse_json(raw)
                    Draft202012Validator.check_schema(schema)
                    validators[name] = Draft202012Validator(
                        schema, format_checker=FormatChecker()
                    )
            for name, expected in manifest["transport_schemas"].items():
                raw = (root / name).read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError("Prototype Landing transport digest differs")
                schema = parse_json(raw)
                Draft202012Validator.check_schema(schema)
                validators[name] = Draft202012Validator(
                    schema, format_checker=FormatChecker()
                )
            for path, expected in manifest["source_authority"]["files"].items():
                raw = (root / Path(path).name).read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError("Prototype Studio source contract digest differs")
                schema = parse_json(raw)
                Draft202012Validator.check_schema(schema)
                validators[Path(path).name] = Draft202012Validator(
                    schema, format_checker=FormatChecker()
                )
            contract = _yaml(files["prototype-landing.yaml"])
            contract_errors = sorted(
                validators["prototype-landing.schema.json"].iter_errors(contract),
                key=lambda error: list(error.absolute_path),
            )
            if contract_errors:
                raise ValueError("Prototype Landing policy contract is invalid")
            return cls(manifest=manifest, files=files, validators=validators)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            SchemaError,
            PrototypeLandingRequestError,
        ) as exc:
            raise PrototypeLandingUnavailable(
                "Prototype Landing contract bundle is unavailable or invalid"
            ) from exc

    def validate(self, name: str, value: Any) -> None:
        try:
            validator = self.validators[name]
        except KeyError as exc:
            raise PrototypeLandingUnavailable(
                f"unknown Prototype Landing schema: {name}"
            ) from exc
        errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
        if errors:
            error = errors[0]
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            raise PrototypeLandingRequestError(f"{name}: {path}: {error.message}")
        canonical_bytes(value)


@dataclass(frozen=True)
class PrototypeLandingSnapshot:
    revision: str
    registry: dict[str, Any]
    registry_digest: str
    record_present: bool
    record_digest: str | None
    source_exists: bool
    contract_digests: dict[str, str]


class PrototypeLandingAuthority:
    """Read Prototype Studio truth from one configured committed Git ref."""

    def __init__(
        self,
        repo_root: Path,
        contracts: PrototypeLandingContracts,
        *,
        trusted_ref: str = "refs/remotes/origin/main",
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.contracts = contracts
        self.trusted_ref = trusted_ref

    def _git(self, *args: str, allow_missing: bool = False) -> bytes | None:
        env = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        env.update({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_GRAFT_FILE": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        })
        try:
            result = subprocess.run(
                [
                    "git", "-c", f"safe.directory={self.repo_root}",
                    "-c", f"core.hooksPath={os.devnull}", "-C", str(self.repo_root), *args,
                ],
                check=False,
                capture_output=True,
                timeout=10,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PrototypeLandingUnavailable(
                "committed Prototype Studio authority is unavailable"
            ) from exc
        if result.returncode:
            if allow_missing:
                return None
            raise PrototypeLandingUnavailable(
                "committed Prototype Studio authority is unavailable"
            )
        return result.stdout

    def snapshot(self, prototype_id: str, source_ref: str) -> PrototypeLandingSnapshot:
        slug = prototype_id.removeprefix("prototype:")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", slug):
            raise PrototypeLandingRequestError("Prototype identity is not source-safe")
        revision_raw = self._git("rev-parse", "--verify", f"{self.trusted_ref}^{{commit}}")
        assert revision_raw is not None
        revision = revision_raw.decode().strip()
        if not COMMIT_PATTERN.fullmatch(revision):
            raise PrototypeLandingUnavailable("Prototype Studio authority revision is invalid")
        self._git(
            "merge-base",
            "--is-ancestor",
            self.contracts.manifest["source_authority"]["minimum_commit"],
            revision,
        )

        source_manifest_raw = self._git(
            "show", f"{revision}:contracts/prototype-landing/manifest.json"
        )
        if source_manifest_raw is None:
            raise PrototypeLandingUnavailable("Prototype Studio contract manifest is unavailable")
        try:
            source_manifest = parse_json(source_manifest_raw)
        except PrototypeLandingRequestError as exc:
            raise PrototypeLandingUnavailable(
                "Prototype Studio contract manifest is invalid"
            ) from exc
        for field in ("authority_repo", "authority_commit", "security_review"):
            if source_manifest.get(field) != self.contracts.manifest[field]:
                raise PrototypeLandingUnavailable(
                    "Prototype Studio contract authority differs from the reviewed WGCF bundle"
                )

        contract_digests: dict[str, str] = {}
        for name, expected in self.contracts.files.items():
            raw = self._git("show", f"{revision}:contracts/prototype-landing/{name}")
            if raw != expected:
                raise PrototypeLandingUnavailable(
                    "Prototype Studio contract bundle differs from reviewed WGCF authority"
                )
            contract_digests[name] = "sha256:" + hashlib.sha256(raw).hexdigest()
        for path, expected in self.contracts.manifest["source_authority"]["files"].items():
            raw = self._git("show", f"{revision}:{path}")
            if raw is None or hashlib.sha256(raw).hexdigest() != expected:
                raise PrototypeLandingUnavailable(
                    "Prototype Studio source schema differs from the reviewed WGCF bundle"
                )

        registry_raw = self._git("show", f"{revision}:{REGISTRY_PATH}")
        assert registry_raw is not None
        if len(registry_raw) > 4 * 1024 * 1024:
            raise PrototypeLandingUnavailable("Prototype Studio registry exceeds the snapshot limit")
        registry = _yaml(registry_raw)
        registry_errors = sorted(
            self.contracts.validators["prototype-registry.schema.json"].iter_errors(registry),
            key=lambda error: list(error.absolute_path),
        )
        ids = [item.get("id") for item in registry.get("prototypes", [])]
        if registry_errors or len(ids) != len(set(ids)):
            raise PrototypeLandingUnavailable("Prototype Studio registry shape is invalid")
        matches = [item for item in registry["prototypes"] if item.get("id") == slug]
        if len(matches) > 1:
            raise PrototypeLandingUnavailable("Prototype Studio registry identity is ambiguous")

        record_path = f"{LANDING_RECORD_ROOT}/{slug}/record.json"
        record_raw = self._git("show", f"{revision}:{record_path}", allow_missing=True)
        try:
            record = parse_json(record_raw) if record_raw is not None else None
        except PrototypeLandingRequestError as exc:
            raise PrototypeLandingUnavailable(
                "Prototype Landing source record is invalid"
            ) from exc
        if record is not None and not isinstance(record, dict):
            raise PrototypeLandingUnavailable("Prototype Landing source record is invalid")

        source_path = _studio_source_path(source_ref)
        source_exists = False
        if source_path is not None:
            source_exists = self._git(
                "cat-file", "-e", f"{revision}:{source_path}", allow_missing=True
            ) is not None
        return PrototypeLandingSnapshot(
            revision=revision,
            registry=registry,
            registry_digest=digest(registry),
            record_present=bool(matches or record is not None),
            record_digest=digest(record) if record is not None else None,
            source_exists=source_exists,
            contract_digests=contract_digests,
        )


def _studio_source_path(source_ref: str) -> str | None:
    prefix = "repo://workspace-prototype-studio/"
    if not source_ref.startswith(prefix):
        return None
    value = source_ref.removeprefix(prefix)
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value:
        raise PrototypeLandingRequestError("Studio source reference escapes its authority")
    return path.as_posix()
