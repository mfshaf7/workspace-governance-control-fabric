"""Pinned Delivery ART contract loading and canonical projections.

Workspace Governance remains the schema authority. This module verifies the
repo-local runtime snapshot against its digest manifest before using it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .canonical_json import canonical_digest, canonicalization_errors


DEFAULT_CONTRACT_ROOT = (
    Path(__file__).resolve().parents[4] / "contracts" / "delivery-art"
)
DELIVERY_ART_CONTRACT_ROOT_ENV = "WGCF_DELIVERY_ART_CONTRACT_ROOT"


class DeliveryArtContractError(ValueError):
    """The pinned authority bundle or an artifact violates its contract."""


@dataclass(frozen=True)
class DeliveryArtContractBundle:
    """Digest-verified schema snapshot consumed by the WGCF runtime."""

    root: Path
    source_commit: str
    validators: dict[str, Draft202012Validator]

    @classmethod
    def load(
        cls,
        root: str | Path | None = None,
    ) -> DeliveryArtContractBundle:
        configured_root = root or os.environ.get(DELIVERY_ART_CONTRACT_ROOT_ENV) or DEFAULT_CONTRACT_ROOT
        resolved_root = Path(configured_root).resolve()
        manifest_path = resolved_root / "manifest.json"
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes)
        except (OSError, json.JSONDecodeError) as exc:
            raise DeliveryArtContractError(
                "Delivery ART authority manifest is unavailable or invalid",
            ) from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise DeliveryArtContractError("unsupported Delivery ART authority manifest")
        source = manifest.get("source")
        if (
            not isinstance(source, dict)
            or source.get("repo") != "workspace-governance"
            or not isinstance(source.get("commit"), str)
            or len(source["commit"]) != 40
        ):
            raise DeliveryArtContractError("Delivery ART authority source is not pinned")
        schema_entries = manifest.get("schemas")
        if not isinstance(schema_entries, dict) or not schema_entries:
            raise DeliveryArtContractError("Delivery ART authority manifest has no schemas")

        format_checker = FormatChecker()
        validators: dict[str, Draft202012Validator] = {}
        for artifact_type, entry in schema_entries.items():
            if not isinstance(artifact_type, str) or not isinstance(entry, dict):
                raise DeliveryArtContractError("Delivery ART schema manifest is malformed")
            schema_path = resolved_root / str(entry.get("path", ""))
            try:
                schema_bytes = schema_path.read_bytes()
            except OSError as exc:
                raise DeliveryArtContractError(
                    f"Delivery ART schema snapshot is unavailable: {schema_path.name}",
                ) from exc
            expected_digest = entry.get("sha256")
            actual_digest = hashlib.sha256(schema_bytes).hexdigest()
            if expected_digest != actual_digest:
                raise DeliveryArtContractError(
                    f"Delivery ART schema snapshot does not match manifest: {schema_path.name}",
                )
            try:
                schema = json.loads(schema_bytes)
                Draft202012Validator.check_schema(schema)
            except (json.JSONDecodeError, SchemaError) as exc:
                raise DeliveryArtContractError(
                    f"Delivery ART schema snapshot is invalid: {schema_path.name}",
                ) from exc
            validators[artifact_type] = Draft202012Validator(
                schema,
                format_checker=format_checker,
            )
        return cls(
            root=resolved_root,
            source_commit=source["commit"],
            validators=validators,
        )

    def validation_errors(self, artifact: Any) -> tuple[str, ...]:
        canonical_errors = canonicalization_errors(artifact)
        if canonical_errors:
            return tuple(canonical_errors)
        if not isinstance(artifact, dict):
            return ("artifact must be an object",)
        validator = self.validators.get(str(artifact.get("artifact_type", "")))
        if validator is None:
            return (f"unsupported artifact_type {artifact.get('artifact_type')!r}",)
        errors = sorted(
            validator.iter_errors(artifact),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        return tuple(
            f"{_json_path(error.absolute_path)}: {error.message}"
            for error in errors
        )

    def require_valid(self, artifact: Any) -> None:
        errors = self.validation_errors(artifact)
        if errors:
            raise DeliveryArtContractError(errors[0])


def review_packet_readiness_subject_digest(packet: dict[str, Any]) -> str:
    """Return the cycle-safe operating-readiness subject digest."""

    projection = copy.deepcopy(packet)
    projection.pop("custody", None)
    projection.pop("finalized_at", None)
    projection.pop("integrity", None)
    readiness = projection.get("readiness")
    if isinstance(readiness, dict):
        readiness.pop("evaluated_at", None)
        readiness.pop("receipt_refs", None)
        readiness.pop("subject_digest", None)
    return canonical_digest(projection)


def operating_readiness_subject(candidate: dict[str, Any]) -> dict[str, Any]:
    """Build the timestamp-free semantic subject evaluated before finalization."""

    subject = copy.deepcopy(candidate)
    subject["status"] = "finalized"
    subject["finalized_at"] = None
    subject["readiness"] = {
        "evaluated_at": None,
        "level": "operating-ready",
        "receipt_refs": [],
        "subject_digest": None,
    }
    subject["readiness"]["subject_digest"] = review_packet_readiness_subject_digest(
        subject,
    )
    return subject


def _json_path(parts: Any) -> str:
    rendered = "$"
    for part in parts:
        rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
    return rendered
