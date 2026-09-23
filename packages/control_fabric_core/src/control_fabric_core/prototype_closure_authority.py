"""Pinned Closure contract and committed Prototype Studio readback."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
import yaml

def prototype_closure_bundle_root(
    env: Mapping[str, str] = os.environ,
) -> Path:
    configured = env.get("WGCF_PROTOTYPE_CLOSURE_CONTRACT_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[4] / "contracts" / "prototype-closure"


BUNDLE_ROOT = prototype_closure_bundle_root()
SAFE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_HISTORY_EVENTS = 100


def studio_digest(value: Any) -> str:
    """Match Prototype Studio's Closure content_digest byte-for-byte."""
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def load_bundle_manifest() -> dict[str, Any]:
    try:
        manifest = json.loads((BUNDLE_ROOT / "manifest.json").read_bytes())
    except (OSError, ValueError) as exc:
        raise PrototypeClosureUnavailable("Prototype Closure bundle is unavailable") from exc
    source = manifest.get("source_authority", {})
    security = manifest.get("security_review", {})
    if (
        manifest.get("contract_id") != "wgcf.prototype-closure-readiness.v1"
        or manifest.get("authority_repo") != "workspace-governance"
        or not COMMIT.fullmatch(str(manifest.get("authority_commit", "")))
        or source.get("repo") != "workspace-prototype-studio"
        or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("contract_manifest_sha256", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("registry_schema_sha256", "")))
        or not re.fullmatch(r"[0-9a-f]{64}", str(source.get("retention_plan_schema_sha256", "")))
        or security.get("repo") != "security-architecture"
        or security.get("decision") != "approved-with-findings"
        or not COMMIT.fullmatch(str(security.get("merge_commit", "")))
        or manifest.get("runtime_activation") is not True
        or set(manifest.get("transport_schemas", {}))
        != {"evaluation.schema.json", "readiness.schema.json"}
    ):
        raise PrototypeClosureUnavailable("Prototype Closure bundle is invalid")
    return manifest


class PrototypeClosureRequestError(ValueError):
    """Invalid request shape or identity."""


class PrototypeClosureUnavailable(RuntimeError):
    """Trusted source or evidence authority is unavailable."""


@dataclass(frozen=True)
class ClosureSource:
    revision: str
    record: dict[str, Any]
    record_digest: str
    lifecycle: str
    custody: str
    history_digest: str | None
    last_event: dict[str, Any] | None


class PrototypeClosureAuthority:
    """Read one committed revision, never the mutable Studio worktree."""

    def __init__(self, repo_root: Path, *, trusted_ref: str = "refs/remotes/origin/main") -> None:
        self.repo_root = repo_root.resolve()
        self.trusted_ref = trusted_ref

    def _git(self, *args: str, missing_ok: bool = False) -> bytes | None:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        })
        try:
            result = subprocess.run(
                ["git", "-c", f"safe.directory={self.repo_root}", "-c", f"core.hooksPath={os.devnull}",
                 "-C", str(self.repo_root), *args],
                check=False, capture_output=True, timeout=10, env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PrototypeClosureUnavailable("committed Studio authority is unavailable") from exc
        if result.returncode:
            if missing_ok:
                return None
            raise PrototypeClosureUnavailable("committed Studio authority is unavailable")
        if len(result.stdout) > MAX_SOURCE_BYTES:
            raise PrototypeClosureUnavailable("Studio authority file exceeds the limit")
        return result.stdout

    def _read(self, revision: str, path: str, *, missing_ok: bool = False) -> bytes | None:
        return self._git("show", f"{revision}:{path}", missing_ok=missing_ok)

    def _json(self, revision: str, path: str) -> dict[str, Any]:
        raw = self._read(revision, path)
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise PrototypeClosureUnavailable("committed Studio JSON is invalid") from exc
        if not isinstance(value, dict):
            raise PrototypeClosureUnavailable("committed Studio JSON is not an object")
        return value

    def _validator(self, revision: str, path: str, expected_sha256: str) -> Draft202012Validator:
        raw = self._read(revision, path)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise PrototypeClosureUnavailable("Closure schema changed without a reviewed bundle")
        try:
            schema = json.loads(raw)
            Draft202012Validator.check_schema(schema)
            return Draft202012Validator(schema, format_checker=FormatChecker())
        except (ValueError, TypeError, SchemaError) as exc:
            raise PrototypeClosureUnavailable("Closure schema is invalid") from exc

    @staticmethod
    def _validate(validator: Draft202012Validator, value: Any, label: str) -> None:
        errors = list(validator.iter_errors(value))
        if errors:
            raise PrototypeClosureRequestError(f"{label} violates pinned schema: {errors[0].message}")

    def contract(self, revision: str) -> tuple[dict[str, Any], dict[str, Draft202012Validator], dict[str, Any]]:
        bundle = load_bundle_manifest()
        manifest_raw = self._read(revision, "contracts/prototype-closure/manifest.json")
        if hashlib.sha256(manifest_raw).hexdigest() != bundle["source_authority"]["contract_manifest_sha256"]:
            raise PrototypeClosureUnavailable("Closure contract manifest changed without review")
        manifest = json.loads(manifest_raw)
        if (
            manifest.get("authority_repo") != "workspace-governance"
            or manifest.get("authority_commit") != bundle["authority_commit"]
            or manifest.get("security_review", {}).get("decision") != "approved-with-findings"
            or manifest.get("security_review", {}).get("merge_commit") != bundle["security_review"]["merge_commit"]
        ):
            raise PrototypeClosureUnavailable("Closure authority manifest is invalid")
        expected = manifest.get("files", {})
        if set(expected) != {
            "prototype-closure.yaml", "prototype-closure-request.schema.json",
            "prototype-closure-history-event.schema.json", "prototype-closure-studio-readback.schema.json",
        }:
            raise PrototypeClosureUnavailable("Closure contract bundle is incomplete")
        for name, sha in expected.items():
            raw = self._read(revision, f"contracts/prototype-closure/{name}")
            if hashlib.sha256(raw).hexdigest() != sha:
                raise PrototypeClosureUnavailable("Closure contract bundle differs from pinned authority")
        try:
            policy = yaml.safe_load(self._read(revision, "contracts/prototype-closure/prototype-closure.yaml"))
        except yaml.YAMLError as exc:
            raise PrototypeClosureUnavailable("Closure policy is invalid") from exc
        if not isinstance(policy, dict) or policy.get("schema_version") != 2 or policy.get("owner_repo") != "workspace-governance":
            raise PrototypeClosureUnavailable("Closure policy authority is invalid")
        validators = {
            name: self._validator(revision, f"contracts/prototype-closure/{name}", expected[name])
            for name in expected if name.endswith(".json")
        }
        validators["prototype-registry.schema.json"] = self._validator(
            revision, "schemas/prototype-registry.schema.json",
            bundle["source_authority"]["registry_schema_sha256"],
        )
        return manifest, validators, policy

    def snapshot(self, prototype_id: str) -> ClosureSource:
        if not SAFE_ID.fullmatch(prototype_id):
            raise PrototypeClosureRequestError("prototype_id is not source-safe")
        revision = self.current_revision()
        _, validators, _ = self.contract(revision)
        try:
            registry = yaml.safe_load(self._read(revision, "prototypes.yaml"))
        except yaml.YAMLError as exc:
            raise PrototypeClosureUnavailable("committed Prototype registry is invalid") from exc
        try:
            self._validate(validators["prototype-registry.schema.json"], registry, "registry")
        except PrototypeClosureRequestError as exc:
            raise PrototypeClosureUnavailable("committed Prototype registry violates its schema") from exc
        matches = [item for item in registry["prototypes"] if item["id"] == prototype_id]
        if len(matches) != 1:
            raise PrototypeClosureRequestError("Prototype identity is absent or ambiguous")
        record = matches[0]
        custody = record.get("source_custody", "incubation-repo")
        if record["lifecycle"] == "graduated" and "source_custody" not in record:
            raise PrototypeClosureUnavailable("graduated source custody is not explicit")
        prefix = f"records/prototype-closure/{prototype_id}/history/"
        names_raw = self._git("ls-tree", "-r", "--name-only", revision, "--", prefix)
        names = [name for name in names_raw.decode().splitlines() if name.startswith(prefix)]
        if len(names) > MAX_HISTORY_EVENTS:
            raise PrototypeClosureUnavailable("Closure history exceeds the evaluation limit")
        prior_digest: str | None = None
        last_event: dict[str, Any] | None = None
        for index, name in enumerate(names, start=1):
            if name != f"{prefix}{index:04d}.json":
                raise PrototypeClosureUnavailable("Closure history sequence is invalid")
            event = self._json(revision, name)
            try:
                self._validate(validators["prototype-closure-history-event.schema.json"], event, "history")
            except PrototypeClosureRequestError as exc:
                raise PrototypeClosureUnavailable("committed Closure history violates its schema") from exc
            if (
                event["prototype_id"] != prototype_id
                or event["event_id"] != f"prototype-closure:{prototype_id}:{index:04d}"
                or event["prior_event_digest"] != prior_digest
            ):
                raise PrototypeClosureUnavailable("Closure history chain is invalid")
            prior_revision = event["expected_source_revision"]
            if not COMMIT.fullmatch(prior_revision):
                raise PrototypeClosureUnavailable("Closure history source revision is invalid")
            self._git("merge-base", "--is-ancestor", prior_revision, revision)
            try:
                prior_registry = yaml.safe_load(self._read(prior_revision, "prototypes.yaml"))
                prior_items = [item for item in prior_registry["prototypes"] if item["id"] == prototype_id]
            except (TypeError, KeyError, yaml.YAMLError) as exc:
                raise PrototypeClosureUnavailable("Closure history prior source is invalid") from exc
            if len(prior_items) != 1:
                raise PrototypeClosureUnavailable("Closure history prior source is ambiguous")
            prior_item = prior_items[0]
            if (
                prior_item.get("lifecycle") != event["previous_lifecycle"]
                or prior_item.get("source_custody", "incubation-repo")
                != event["previous_source_custody"]
            ):
                raise PrototypeClosureUnavailable("Closure history prior state disagrees with source")
            prior_digest = studio_digest(event)
            last_event = event
        if last_event is not None:
            expected_ref = f"record://prototype-closure/{prototype_id}/history/{last_event['event_id']}"
            if (
                record.get("closure_event_ref") != expected_ref
                or record["lifecycle"] != last_event["observed_lifecycle"]
                or custody != last_event["observed_source_custody"]
            ):
                raise PrototypeClosureUnavailable("Closure registry and history disagree")
        return ClosureSource(
            revision, record, studio_digest(record), record["lifecycle"], custody, prior_digest, last_event
        )

    def current_revision(self) -> str:
        raw = self._git("rev-parse", "--verify", f"{self.trusted_ref}^{{commit}}")
        revision = raw.decode().strip()
        if not COMMIT.fullmatch(revision):
            raise PrototypeClosureUnavailable("Studio authority revision is invalid")
        return revision

    def validate_request(self, revision: str, request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        manifest, validators, policy = self.contract(revision)
        self._validate(validators["prototype-closure-request.schema.json"], request, "request")
        if not SAFE_ID.fullmatch(request["prototype_id"]):
            raise PrototypeClosureRequestError("prototype_id is not source-safe")
        return manifest, policy
