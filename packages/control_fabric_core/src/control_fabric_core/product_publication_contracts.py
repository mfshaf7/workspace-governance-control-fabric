"""Pinned Product Publication contracts and exact committed authority readers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from .record_authority_freshness import require_record_authority_freshness

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .workspace_intake_contracts import (
    IntakeRequestError,
    artifact_digest as _artifact_digest,
    canonical_bytes as _canonical_bytes,
    digest as _digest,
    parse_json as _parse_json,
    yaml_record,
)
from .workspace_inventory_contracts import InventoryUnavailable, validate_history


CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "contracts" / "product-publication"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REPO_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_AUTHORITY_BYTES = 4 * 1024 * 1024


class ProductPublicationRequestError(ValueError):
    """Invalid transport or artifact integrity, not a readiness decision."""


class ProductPublicationUnavailable(RuntimeError):
    """Pinned contracts, committed authority, or durable evidence are unavailable."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return _canonical_bytes(value)
    except IntakeRequestError as exc:
        raise ProductPublicationRequestError(str(exc)) from exc


def parse_json(raw: bytes) -> Any:
    try:
        return _parse_json(raw)
    except (IntakeRequestError, UnicodeDecodeError) as exc:
        raise ProductPublicationRequestError(str(exc)) from exc


def digest(value: Any) -> str:
    try:
        return _digest(value)
    except IntakeRequestError as exc:
        raise ProductPublicationRequestError(str(exc)) from exc


def artifact_digest(value: dict[str, Any], field: str) -> str:
    try:
        return _artifact_digest(value, field)
    except IntakeRequestError as exc:
        raise ProductPublicationRequestError(str(exc)) from exc


@dataclass(frozen=True)
class ProductPublicationContracts:
    manifest: dict[str, Any]
    validators: dict[str, Draft202012Validator]

    @classmethod
    def load(cls, root: Path | None = None) -> "ProductPublicationContracts":
        root = Path(root or os.environ.get("WGCF_PRODUCT_PUBLICATION_CONTRACT_ROOT") or CONTRACT_ROOT)
        try:
            manifest = parse_json((root / "manifest.json").read_bytes())
            if (
                manifest["contract_id"] != "wgcf.product-publication-readiness.v1"
                or manifest["authority_repo"] != "workspace-governance"
                or not COMMIT_PATTERN.fullmatch(manifest["authority_commit"])
                or set(manifest["authority_files"]) != {
                    "contracts/product-publication.yaml",
                    "contracts/schemas/product-publication-manifest.schema.json",
                    "contracts/schemas/workspace-inventory-history.schema.json",
                }
                or set(manifest["transport_schemas"]) != {
                    "evaluation.schema.json",
                    "readiness.schema.json",
                }
            ):
                raise ValueError("invalid Product Publication bundle manifest")
            validators: dict[str, Draft202012Validator] = {}
            for name, expected in manifest["transport_schemas"].items():
                raw = (root / name).read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError(f"Product Publication schema digest differs for {name}")
                schema = parse_json(raw)
                Draft202012Validator.check_schema(schema)
                validators[name] = Draft202012Validator(schema, format_checker=FormatChecker())
            return cls(manifest=manifest, validators=validators)
        except (OSError, ValueError, KeyError, TypeError, SchemaError) as exc:
            raise ProductPublicationUnavailable(
                "Product Publication contract bundle is unavailable or invalid"
            ) from exc

    def validate(self, name: str, value: Any) -> None:
        try:
            validator = self.validators[name]
        except KeyError as exc:
            raise ProductPublicationUnavailable(f"unknown Product Publication schema: {name}") from exc
        errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
        if errors:
            error = errors[0]
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            raise ProductPublicationRequestError(f"{name}: {path}: {error.message}")
        canonical_bytes(value)


@dataclass(frozen=True)
class ProductPublicationSnapshot:
    authority_revision: str
    products: dict[str, Any]
    products_digest: str
    history: dict[str, Any]
    history_digest: str
    policy: dict[str, Any]
    contract_digest: str
    manifest: dict[str, Any]
    manifest_digest: str
    manifest_revision: str
    manifest_path: str
    owner_head: str


class ProductPublicationAuthority:
    """Resolve Workspace Governance policy and one owner manifest from committed Git."""

    def __init__(
        self,
        workspace_repo_root: Path,
        owner_repo_roots: dict[str, Path],
        contracts: ProductPublicationContracts,
        *,
        trusted_ref: str = "refs/remotes/origin/main",
    ) -> None:
        self.workspace_root = workspace_repo_root.resolve()
        self.owner_roots = {name: path.resolve() for name, path in owner_repo_roots.items()}
        self.contracts = contracts
        self.trusted_ref = trusted_ref

    @staticmethod
    def _git(repo: Path, *args: str, allow_failure: bool = False) -> bytes | None:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
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
                ["git", "-c", f"safe.directory={repo}", "-c", f"core.hooksPath={os.devnull}", "-C", str(repo), *args],
                check=False,
                capture_output=True,
                timeout=10,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProductPublicationUnavailable("committed Product Publication authority is unavailable") from exc
        if result.returncode:
            if allow_failure:
                return None
            raise ProductPublicationUnavailable("committed Product Publication authority is unavailable")
        return result.stdout

    def snapshot(self, source: dict[str, Any]) -> ProductPublicationSnapshot:
        repo_name = source["repo"]
        if not REPO_PATTERN.fullmatch(repo_name) or repo_name not in self.owner_roots:
            raise ProductPublicationRequestError("manifest owner repository is not configured")
        workspace_revision = self._commit(self.workspace_root, self.trusted_ref)
        require_record_authority_freshness(self.workspace_root, "workspace-governance", workspace_revision,
            trusted_ref=self.trusted_ref, unavailable=ProductPublicationUnavailable)
        self._git(
            self.workspace_root,
            "merge-base",
            "--is-ancestor",
            self.contracts.manifest["authority_commit"],
            workspace_revision,
        )
        authority_raw: dict[str, bytes] = {}
        for path, expected in self.contracts.manifest["authority_files"].items():
            raw = self._read(self.workspace_root, workspace_revision, path)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ProductPublicationUnavailable(
                    "Product Publication authority changed; refresh the reviewed bundle"
                )
            authority_raw[path] = raw
        products_raw = self._read(self.workspace_root, workspace_revision, "contracts/products.yaml")
        history_raw = self._read(
            self.workspace_root,
            workspace_revision,
            "contracts/workspace-inventory-history.yaml",
        )
        policy = yaml_record(authority_raw["contracts/product-publication.yaml"])
        products = yaml_record(products_raw)
        history = yaml_record(history_raw)
        manifest_schema = parse_json(
            authority_raw["contracts/schemas/product-publication-manifest.schema.json"]
        )
        try:
            Draft202012Validator.check_schema(manifest_schema)
            manifest_validator = Draft202012Validator(
                manifest_schema, format_checker=FormatChecker()
            )
        except SchemaError as exc:
            raise ProductPublicationUnavailable("Product Publication manifest schema is invalid") from exc
        history_schema = parse_json(
            authority_raw["contracts/schemas/workspace-inventory-history.schema.json"]
        )
        try:
            Draft202012Validator.check_schema(history_schema)
            history_validator = Draft202012Validator(
                history_schema, format_checker=FormatChecker()
            )
        except SchemaError as exc:
            raise ProductPublicationUnavailable(
                "Workspace Inventory history schema is invalid"
            ) from exc
        if next(history_validator.iter_errors(history), None) is not None:
            raise ProductPublicationUnavailable(
                "committed Workspace Inventory history is invalid"
            )
        try:
            validate_history(history)
        except InventoryUnavailable as exc:
            raise ProductPublicationUnavailable(
                "committed Workspace Inventory history is invalid"
            ) from exc

        owner_root = self.owner_roots[repo_name]
        owner_head = self._commit(owner_root, self.trusted_ref)
        if repo_name in {"workspace-governance", "workspace-prototype-studio"}:
            require_record_authority_freshness(owner_root, repo_name, owner_head,
                trusted_ref=self.trusted_ref, unavailable=ProductPublicationUnavailable)
        manifest_revision = source["manifest_revision"]
        source_revision = source["source_revision"]
        if self._git(owner_root, "cat-file", "-e", f"{manifest_revision}^{{commit}}", allow_failure=True) is None:
            raise ProductPublicationRequestError("manifest revision is absent from owner repository")
        if self._git(owner_root, "merge-base", "--is-ancestor", source_revision, manifest_revision, allow_failure=True) is None:
            raise ProductPublicationRequestError("manifest revision does not descend from owner source revision")
        if self._git(owner_root, "merge-base", "--is-ancestor", manifest_revision, owner_head, allow_failure=True) is None:
            raise ProductPublicationRequestError("manifest revision is outside trusted owner history")
        manifest_raw = self._read(owner_root, manifest_revision, source["path"])
        manifest = parse_json(manifest_raw)
        errors = sorted(manifest_validator.iter_errors(manifest), key=lambda error: list(error.absolute_path))
        if errors:
            error = errors[0]
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            raise ProductPublicationRequestError(f"owner manifest: {path}: {error.message}")
        manifest_digest = digest(manifest)
        return ProductPublicationSnapshot(
            authority_revision=workspace_revision,
            products=products,
            products_digest=digest(products),
            history=history,
            history_digest=digest(history),
            policy=policy,
            contract_digest="sha256:" + self.contracts.manifest["authority_files"]["contracts/product-publication.yaml"],
            manifest=manifest,
            manifest_digest=manifest_digest,
            manifest_revision=manifest_revision,
            manifest_path=source["path"],
            owner_head=owner_head,
        )

    def _commit(self, repo: Path, ref: str) -> str:
        raw = self._git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        assert raw is not None
        value = raw.decode().strip()
        if not COMMIT_PATTERN.fullmatch(value):
            raise ProductPublicationUnavailable("Product Publication authority revision is invalid")
        return value

    def _read(self, repo: Path, revision: str, path: str) -> bytes:
        raw = self._git(repo, "show", f"{revision}:{path}")
        assert raw is not None
        if len(raw) > MAX_AUTHORITY_BYTES:
            raise ProductPublicationUnavailable("Product Publication authority exceeds the size limit")
        return raw


def owner_roots_from_json(raw: str) -> dict[str, Path]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProductPublicationUnavailable("Product Publication owner repository map is invalid") from exc
    if not isinstance(value, dict) or not value:
        raise ProductPublicationUnavailable("Product Publication owner repository map is empty")
    roots: dict[str, Path] = {}
    for name, path in value.items():
        if not isinstance(name, str) or not REPO_PATTERN.fullmatch(name) or not isinstance(path, str) or not path:
            raise ProductPublicationUnavailable("Product Publication owner repository map is invalid")
        roots[name] = Path(path)
    return roots
