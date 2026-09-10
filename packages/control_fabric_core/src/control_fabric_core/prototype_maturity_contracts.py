"""Pinned Prototype maturity contracts and committed source authority."""

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
    yaml_record,
)


CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "contracts" / "prototype-maturity"
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
AUTHORITY_FILES = {
    "prototype-maturity.yaml",
    "prototype-maturity.schema.json",
    "prototype-maturity-request.schema.json",
    "prototype-maturity-packet.schema.json",
    "prototype-maturity-readiness.schema.json",
    "prototype-maturity-decision.schema.json",
    "prototype-maturity-readback.schema.json",
    "prototype-maturity-receipt.schema.json",
}
SOURCE_FILES = {
    "schemas/prototype-registry.schema.json",
    "schemas/prototype-candidate-record.schema.json",
}
MAX_AUTHORITY_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_REFS = 100


class PrototypeMaturityRequestError(ValueError):
    """Invalid transport or artifact integrity, not a readiness decision."""


class PrototypeMaturityUnavailable(RuntimeError):
    """Pinned contracts, source authority, or durable evidence are unavailable."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return _canonical_bytes(value)
    except IntakeRequestError as exc:
        raise PrototypeMaturityRequestError(str(exc)) from exc


def parse_json(raw: bytes) -> Any:
    try:
        return _parse_json(raw)
    except (IntakeRequestError, UnicodeDecodeError) as exc:
        raise PrototypeMaturityRequestError(str(exc)) from exc


def digest(value: Any) -> str:
    try:
        return _digest(value)
    except IntakeRequestError as exc:
        raise PrototypeMaturityRequestError(str(exc)) from exc


def artifact_digest(value: dict[str, Any], field: str) -> str:
    try:
        return _artifact_digest(value, field)
    except IntakeRequestError as exc:
        raise PrototypeMaturityRequestError(str(exc)) from exc


def _durable_artifact_ref(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    digest_value = value.get("digest")
    return bool(
        isinstance(digest_value, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", digest_value)
        and value.get("uri")
        == "wgcf://artifacts/delivery-art/" + digest_value.replace(":", "/", 1)
    )


@dataclass(frozen=True)
class PrototypeMaturityContracts:
    manifest: dict[str, Any]
    files: dict[str, bytes]
    validators: dict[str, Draft202012Validator]
    policy: dict[str, Any]

    @classmethod
    def load(cls, root: Path | None = None) -> "PrototypeMaturityContracts":
        root = Path(root or os.environ.get("WGCF_PROTOTYPE_MATURITY_CONTRACT_ROOT") or CONTRACT_ROOT)
        try:
            manifest = parse_json((root / "manifest.json").read_bytes())
            security = manifest["security_review"]
            activation_review = manifest["activation_review"]
            activation_evidence = manifest["activation_evidence"]
            conformance_packet = activation_evidence["conformance_review_packet"]
            identity_packet = activation_evidence["identity_review_packet"]
            identity_definition = activation_evidence["identity_definition"]
            source = manifest["source_authority"]
            if (
                manifest["contract_id"] != "wgcf.prototype-maturity-readiness.v1"
                or manifest["authority_repo"] != "workspace-governance"
                or not COMMIT_PATTERN.fullmatch(manifest["authority_commit"])
                or security["repo"] != "security-architecture"
                or not COMMIT_PATTERN.fullmatch(security["commit"])
                or security["decision"] != "approved-with-findings"
                or not security["path"].startswith("docs/reviews/components/")
                or not re.fullmatch(r"[0-9a-f]{64}", security["content_sha256"])
                or activation_review["repo"] != "security-architecture"
                or not COMMIT_PATTERN.fullmatch(activation_review["commit"])
                or not activation_review["path"].startswith("docs/reviews/components/")
                or not activation_review["path"].endswith(".md")
                or not re.fullmatch(r"[0-9a-f]{64}", activation_review["content_sha256"])
                or activation_review["decision"] != "approved-with-findings"
                or not _durable_artifact_ref(conformance_packet)
                or not _durable_artifact_ref(identity_packet)
                or identity_definition["repo"] != "platform-engineering"
                or not COMMIT_PATTERN.fullmatch(identity_definition["commit"])
                or identity_definition["path"] != "security/prototype-maturity-identity.yaml"
                or not re.fullmatch(r"[0-9a-f]{64}", identity_definition["content_sha256"])
                or source["repo"] != "workspace-prototype-studio"
                or not COMMIT_PATTERN.fullmatch(source["minimum_commit"])
                or not re.fullmatch(r"[0-9a-f]{64}", source["contract_manifest_sha256"])
                or manifest["runtime_activation"] is not True
            ):
                raise ValueError("invalid Prototype maturity bundle manifest")
            if set(manifest["files"]) != AUTHORITY_FILES:
                raise ValueError("incomplete Prototype maturity authority bundle")
            if set(manifest["transport_schemas"]) != {"evaluation.schema.json"}:
                raise ValueError("incomplete Prototype maturity transport bundle")
            if set(source["files"]) != SOURCE_FILES:
                raise ValueError("incomplete Prototype maturity source bundle")

            files: dict[str, bytes] = {}
            validators: dict[str, Draft202012Validator] = {}
            for name, expected in {
                **manifest["files"],
                **manifest["transport_schemas"],
            }.items():
                raw = (root / name).read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError(f"Prototype maturity contract digest differs for {name}")
                files[name] = raw
                if name.endswith(".json"):
                    schema = parse_json(raw)
                    Draft202012Validator.check_schema(schema)
                    validators[name] = Draft202012Validator(
                        schema, format_checker=FormatChecker()
                    )
            for path, expected in source["files"].items():
                name = Path(path).name
                raw = (root / name).read_bytes()
                if hashlib.sha256(raw).hexdigest() != expected:
                    raise ValueError(f"Prototype maturity source digest differs for {path}")
                schema = parse_json(raw)
                Draft202012Validator.check_schema(schema)
                validators[name] = Draft202012Validator(
                    schema, format_checker=FormatChecker()
                )
            policy = yaml_record(files["prototype-maturity.yaml"])
            errors = sorted(
                validators["prototype-maturity.schema.json"].iter_errors(policy),
                key=lambda error: list(error.absolute_path),
            )
            if errors:
                raise ValueError("Prototype maturity policy contract is invalid")
            return cls(manifest, files, validators, policy)
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            SchemaError,
            PrototypeMaturityRequestError,
            IntakeUnavailable,
        ) as exc:
            raise PrototypeMaturityUnavailable(
                "Prototype maturity contract bundle is unavailable or invalid"
            ) from exc

    def validate(self, name: str, value: Any) -> None:
        try:
            validator = self.validators[name]
        except KeyError as exc:
            raise PrototypeMaturityUnavailable(
                f"unknown Prototype maturity schema: {name}"
            ) from exc
        errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
        if errors:
            error = errors[0]
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            raise PrototypeMaturityRequestError(f"{name}: {path}: {error.message}")
        canonical_bytes(value)

    @property
    def policy_ref(self) -> dict[str, str]:
        return {
            "id": "workspace-governance.prototype-maturity.v1",
            "authority_commit": self.manifest["authority_commit"],
            "digest": "sha256:" + self.manifest["files"]["prototype-maturity.yaml"],
        }

    @property
    def security_review_ref(self) -> dict[str, str]:
        review = self.manifest["security_review"]
        return {
            "repo": review["repo"],
            "commit": review["commit"],
            "path": review["path"],
            "digest": "sha256:" + review["content_sha256"],
            "decision": review["decision"],
        }


@dataclass(frozen=True)
class EvidenceResolution:
    ref: str
    state: str
    evidence_ref: str
    owner_ref: str


@dataclass(frozen=True)
class PrototypeMaturitySnapshot:
    revision: str
    record: dict[str, Any]
    record_digest: str
    lifecycle: str
    landing_record_valid: bool
    candidate_record_valid: bool
    evidence: tuple[EvidenceResolution, ...]


class PrototypeMaturityAuthority:
    """Read maturity and evidence truth from one committed Prototype Studio ref."""

    def __init__(
        self,
        repo_root: Path,
        contracts: PrototypeMaturityContracts,
        *,
        trusted_ref: str = "refs/remotes/origin/main",
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.contracts = contracts
        self.trusted_ref = trusted_ref

    def _git(self, *args: str, allow_missing: bool = False) -> bytes | None:
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
                [
                    "git",
                    "-c",
                    f"safe.directory={self.repo_root}",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "-C",
                    str(self.repo_root),
                    *args,
                ],
                check=False,
                capture_output=True,
                timeout=10,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PrototypeMaturityUnavailable(
                "committed Prototype Studio authority is unavailable"
            ) from exc
        if result.returncode:
            if allow_missing:
                return None
            raise PrototypeMaturityUnavailable(
                "committed Prototype Studio authority is unavailable"
            )
        return result.stdout

    def snapshot(
        self, prototype_id: str, evidence_refs: list[str]
    ) -> PrototypeMaturitySnapshot:
        slug = prototype_id.removeprefix("prototype:")
        if not SAFE_SLUG.fullmatch(slug):
            raise PrototypeMaturityRequestError("Prototype identity is not source-safe")
        if len(evidence_refs) > MAX_EVIDENCE_REFS:
            raise PrototypeMaturityRequestError("Prototype maturity evidence exceeds the limit")

        revision_raw = self._git("rev-parse", "--verify", f"{self.trusted_ref}^{{commit}}")
        assert revision_raw is not None
        revision = revision_raw.decode().strip()
        if not COMMIT_PATTERN.fullmatch(revision):
            raise PrototypeMaturityUnavailable("Prototype Studio authority revision is invalid")
        self._git(
            "merge-base",
            "--is-ancestor",
            self.contracts.manifest["source_authority"]["minimum_commit"],
            revision,
        )
        self._verify_source_bundle(revision)

        registry_raw = self._read(revision, "prototypes.yaml")
        registry = yaml_record(registry_raw)
        try:
            self.contracts.validate("prototype-registry.schema.json", registry)
        except PrototypeMaturityRequestError as exc:
            raise PrototypeMaturityUnavailable(
                "committed Prototype registry is invalid"
            ) from exc
        matches = [item for item in registry["prototypes"] if item.get("id") == slug]
        if len(matches) != 1:
            raise PrototypeMaturityRequestError(
                "Prototype identity is absent or ambiguous in committed Studio truth"
            )
        record = matches[0]
        landing_valid = self._landing_record_valid(revision, slug, record)
        candidate_valid = self._candidate_record_valid(revision, slug, record)
        evidence = tuple(self._resolve_evidence(revision, ref) for ref in sorted(set(evidence_refs)))
        return PrototypeMaturitySnapshot(
            revision=revision,
            record=record,
            record_digest=digest(record),
            lifecycle=str(record.get("lifecycle") or ""),
            landing_record_valid=landing_valid,
            candidate_record_valid=candidate_valid,
            evidence=evidence,
        )

    def _verify_source_bundle(self, revision: str) -> None:
        source = self.contracts.manifest["source_authority"]
        manifest_raw = self._read(revision, source["contract_manifest_path"])
        if hashlib.sha256(manifest_raw).hexdigest() != source["contract_manifest_sha256"]:
            raise PrototypeMaturityUnavailable(
                "Prototype maturity source contract changed; refresh the reviewed bundle"
            )
        for path, expected in source["files"].items():
            raw = self._read(revision, path)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise PrototypeMaturityUnavailable(
                    "Prototype maturity source schema changed; refresh the reviewed bundle"
                )

    def _read(self, revision: str, path: str, *, allow_missing: bool = False) -> bytes | None:
        raw = self._git("show", f"{revision}:{path}", allow_missing=allow_missing)
        if raw is not None and len(raw) > MAX_AUTHORITY_BYTES:
            raise PrototypeMaturityUnavailable("Prototype maturity authority file exceeds the limit")
        return raw

    def _landing_record_valid(
        self, revision: str, slug: str, record: dict[str, Any]
    ) -> bool:
        expected_ref = f"record://prototype-landings/{slug}"
        linked = [
            link
            for link in record.get("linked_records", [])
            if link.get("role") == "landing-record" and link.get("ref") == expected_ref
        ]
        if record.get("landing_record_ref") != expected_ref or len(linked) != 1:
            return False
        raw = self._read(
            revision, f"records/prototype-landings/{slug}/record.json", allow_missing=True
        )
        if raw is None:
            return False
        try:
            landing = parse_json(raw)
            entry_ref = landing.get("entry_ref")
            source = landing.get("source")
            return (
                landing.get("id") == f"prototype:{slug}"
                and landing.get("lifecycle") == "exploring"
                and landing.get("project_phase") == "incubating"
                and landing.get("next_action") == "candidate-promotion"
                and isinstance(landing.get("name"), str)
                and bool(landing["name"].strip())
                and isinstance(landing.get("objective"), str)
                and bool(landing["objective"].strip())
                and isinstance(landing.get("setup"), dict)
                and isinstance(entry_ref, dict)
                and isinstance(entry_ref.get("id"), str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", str(entry_ref.get("digest")))
                is not None
                and isinstance(source, dict)
                and isinstance(source.get("ref"), str)
                and bool(source["ref"].strip())
            )
        except (PrototypeMaturityRequestError, AttributeError):
            return False

    def _candidate_record_valid(
        self, revision: str, slug: str, record: dict[str, Any]
    ) -> bool:
        expected_ref = f"record://prototype-maturity/{slug}/candidate"
        links = [
            link
            for link in record.get("linked_records", [])
            if link.get("role") == "candidate-record" and link.get("ref") == expected_ref
        ]
        raw = self._read(
            revision, f"records/prototype-maturity/{slug}/candidate.json", allow_missing=True
        )
        if raw is None or len(links) != 1:
            return False
        try:
            candidate = parse_json(raw)
            self.contracts.validate("prototype-candidate-record.schema.json", candidate)
            body = {key: value for key, value in candidate.items() if key != "record_digest"}
            return (
                candidate.get("prototype_id") == f"prototype:{slug}"
                and candidate.get("record_digest") == digest(body)
            )
        except PrototypeMaturityRequestError:
            return False

    def _resolve_evidence(self, revision: str, ref: str) -> EvidenceResolution:
        owner = "workspace-prototype-studio"
        path: str | None = None
        if ref.startswith("repo://workspace-prototype-studio/"):
            path = ref.removeprefix("repo://workspace-prototype-studio/")
        elif ref.startswith("record://prototype-landings/"):
            slug = ref.removeprefix("record://prototype-landings/")
            path = f"records/prototype-landings/{slug}/record.json"
        elif ref.startswith("record://prototype-maturity/"):
            suffix = ref.removeprefix("record://prototype-maturity/")
            path = f"records/prototype-maturity/{suffix}.json"
        elif ref.startswith("record://design-baselines/"):
            slug = ref.removeprefix("record://design-baselines/")
            path = f"records/design-baselines/{slug}.yaml"
        if path is None:
            return EvidenceResolution(ref, "unsupported", f"unresolved:{ref}", "evidence-owner")
        candidate = PurePosixPath(path)
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or "\\" in path
            or len(path) > 512
        ):
            return EvidenceResolution(ref, "invalid", f"invalid:{ref}", owner)
        raw = self._read(revision, str(candidate), allow_missing=True)
        if raw is None:
            return EvidenceResolution(ref, "missing", f"missing:{ref}", owner)
        evidence_ref = f"{ref}@sha256:{hashlib.sha256(raw).hexdigest()}"
        return EvidenceResolution(ref, "verified", evidence_ref, owner)
