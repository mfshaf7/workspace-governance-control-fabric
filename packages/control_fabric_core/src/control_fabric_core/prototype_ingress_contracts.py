"""Digest-pinned contracts for Prototype-to-Delivery ingress readiness."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .canonical_json import canonical_digest, canonicalization_errors


DEFAULT_CONTRACT_ROOT = Path(__file__).resolve().parents[4] / "contracts" / "prototype-ingress"
PROTOTYPE_INGRESS_CONTRACT_ROOT_ENV = "WGCF_PROTOTYPE_INGRESS_CONTRACT_ROOT"


class PrototypeIngressContractError(ValueError):
    """The pinned contract bundle or one of its records is invalid."""


@dataclass(frozen=True)
class PrototypeIngressContractBundle:
    root: Path
    manifest: dict[str, Any]
    contract_digest: str
    validators: dict[str, Draft202012Validator]

    @classmethod
    def load(cls, root: str | Path | None = None) -> PrototypeIngressContractBundle:
        resolved_root = Path(
            root or os.environ.get(PROTOTYPE_INGRESS_CONTRACT_ROOT_ENV) or DEFAULT_CONTRACT_ROOT,
        ).resolve()
        try:
            manifest = json.loads((resolved_root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PrototypeIngressContractError(
                "Prototype ingress contract manifest is unavailable or invalid",
            ) from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise PrototypeIngressContractError("unsupported Prototype ingress contract manifest")
        if manifest.get("contract_id") != "wgcf.prototype-ingress-readiness.v1":
            raise PrototypeIngressContractError("Prototype ingress contract id is invalid")

        source = manifest.get("source_contract")
        if (
            not isinstance(source, dict)
            or source.get("repo") != "workspace-prototype-studio"
            or not isinstance(source.get("commit"), str)
            or len(source["commit"]) != 40
            or source.get("path") != "schemas/prototype-delivery-packet.schema.json"
        ):
            raise PrototypeIngressContractError("Prototype packet source contract is not pinned")

        schema_entries = manifest.get("schemas")
        if not isinstance(schema_entries, dict) or set(schema_entries) != {
            "prototype_delivery_packet",
            "prototype_ingress_readiness_request",
            "prototype_ingress_readiness_receipt",
        }:
            raise PrototypeIngressContractError("Prototype ingress schema manifest is incomplete")

        validators: dict[str, Draft202012Validator] = {}
        format_checker = FormatChecker()
        for record_type, entry in schema_entries.items():
            if not isinstance(entry, dict):
                raise PrototypeIngressContractError("Prototype ingress schema entry is malformed")
            schema_path = resolved_root / str(entry.get("path") or "")
            try:
                schema_bytes = schema_path.read_bytes()
            except OSError as exc:
                raise PrototypeIngressContractError(
                    f"Prototype ingress schema is unavailable: {schema_path.name}",
                ) from exc
            if hashlib.sha256(schema_bytes).hexdigest() != entry.get("sha256"):
                raise PrototypeIngressContractError(
                    f"Prototype ingress schema does not match manifest: {schema_path.name}",
                )
            try:
                schema = json.loads(schema_bytes)
                Draft202012Validator.check_schema(schema)
            except (json.JSONDecodeError, SchemaError) as exc:
                raise PrototypeIngressContractError(
                    f"Prototype ingress schema is invalid: {schema_path.name}",
                ) from exc
            validators[record_type] = Draft202012Validator(
                schema,
                format_checker=format_checker,
            )

        if source.get("sha256") != schema_entries["prototype_delivery_packet"].get("sha256"):
            raise PrototypeIngressContractError("Prototype packet source digest is inconsistent")
        authority_refs = manifest.get("authority_refs")
        if (
            not isinstance(authority_refs, list)
            or len(authority_refs) < 2
            or any(not isinstance(ref, str) or not ref for ref in authority_refs)
        ):
            raise PrototypeIngressContractError("Prototype ingress authority refs are incomplete")
        return cls(
            root=resolved_root,
            manifest=manifest,
            contract_digest=canonical_digest(manifest),
            validators=validators,
        )

    @property
    def authority_refs(self) -> tuple[str, ...]:
        return tuple(self.manifest["authority_refs"])

    def errors(self, record_type: str, record: Any) -> tuple[str, ...]:
        canonical_errors = canonicalization_errors(record)
        if canonical_errors:
            return tuple(canonical_errors)
        validator = self.validators.get(record_type)
        if validator is None:
            return (f"unsupported Prototype ingress record type {record_type!r}",)
        errors = sorted(
            validator.iter_errors(record),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        return tuple(
            f"{_json_path(error.absolute_path)}: {error.message}"
            for error in errors
        )

    def require_valid(self, record_type: str, record: Any) -> None:
        errors = self.errors(record_type, record)
        if errors:
            raise PrototypeIngressContractError("; ".join(errors))


def prototype_packet_digest(content: Any) -> str:
    """Apply the exact canonicalization contract used by Prototype Studio."""

    errors = canonicalization_errors(content)
    if errors:
        raise PrototypeIngressContractError(errors[0])
    body = json.dumps(
        content,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _json_path(path: Any) -> str:
    parts = [str(part) for part in path]
    return ".".join(parts) if parts else "<root>"
