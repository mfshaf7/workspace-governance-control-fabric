"""Canonical JSON helpers for Delivery ART evidence artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


MAX_SAFE_INTEGER = 9_007_199_254_740_991


def strict_json_loads(raw: bytes) -> Any:
    """Parse UTF-8 JSON while rejecting duplicate keys and non-canonical values."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("request body must be valid UTF-8 JSON") from exc

    def reject_constant(value: str) -> None:
        raise ValueError(f"unsupported JSON constant {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"request body is not valid JSON: {exc.msg}") from exc
    errors = canonicalization_errors(value)
    if errors:
        raise ValueError(errors[0])
    return value


def canonicalization_errors(value: Any, path: str = "<root>") -> list[str]:
    """Return violations of the integer-only RFC 8785 contract domain."""

    errors: list[str] = []
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            errors.append(f"{path} contains a lone UTF-16 surrogate")
    elif isinstance(value, float):
        errors.append(
            f"{path} uses a floating-point value; Delivery ART artifacts require integral numbers",
        )
    elif isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > MAX_SAFE_INTEGER:
            errors.append(f"{path} exceeds the RFC 8785 safe integer range")
    elif isinstance(value, list):
        for index, entry in enumerate(value):
            errors.extend(canonicalization_errors(entry, f"{path}[{index}]"))
    elif isinstance(value, dict):
        for key, entry in value.items():
            if not isinstance(key, str):
                errors.append(f"{path} contains a non-string object key")
                continue
            errors.extend(canonicalization_errors(key, f"{path} key"))
            errors.extend(canonicalization_errors(entry, f"{path}[{key!r}]"))
    elif value is not None and not isinstance(value, bool):
        errors.append(f"{path} contains an unsupported canonical JSON value")
    return errors


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize the integer-only RFC 8785 subset used by Delivery ART."""

    errors = canonicalization_errors(value)
    if errors:
        raise ValueError(errors[0])
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if isinstance(value, list):
        return b"[" + b",".join(canonical_json_bytes(entry) for entry in value) + b"]"
    if isinstance(value, dict):
        entries = []
        for key in sorted(value, key=lambda item: item.encode("utf-16be")):
            entries.append(
                canonical_json_bytes(key) + b":" + canonical_json_bytes(value[key]),
            )
        return b"{" + b",".join(entries) + b"}"
    raise ValueError(f"unsupported canonical JSON value {type(value).__name__}")


def sha256_digest(value: bytes) -> str:
    """Return the contract-formatted SHA-256 digest for bytes."""

    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def canonical_digest(value: Any) -> str:
    """Return the SHA-256 digest of canonical JSON bytes."""

    return sha256_digest(canonical_json_bytes(value))


def delivery_art_content_projection(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the workspace-contract content-digest projection."""

    projection = copy.deepcopy(payload)
    custody = projection.pop("custody", None)
    if isinstance(custody, dict) and isinstance(custody.get("supersedes"), dict):
        projection["custody"] = {"supersedes": copy.deepcopy(custody["supersedes"])}
    integrity = projection.get("integrity")
    if isinstance(integrity, dict):
        integrity.pop("content_digest", None)
    return projection
