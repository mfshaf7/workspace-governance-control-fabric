"""S3-compatible immutable object persistence for Delivery ART content."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import os
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .canonical_json import sha256_digest


STORAGE_ENDPOINT_ENV = "WGCF_EVIDENCE_STORAGE_ENDPOINT"
STORAGE_BUCKET_ENV = "WGCF_EVIDENCE_STORAGE_BUCKET"
STORAGE_ACCESS_KEY_ENV = "WGCF_EVIDENCE_STORAGE_ACCESS_KEY"
STORAGE_SECRET_KEY_ENV = "WGCF_EVIDENCE_STORAGE_SECRET_KEY"


class ArtifactStorageError(RuntimeError):
    """Base failure for the bounded evidence object store."""


class ArtifactStorageUnavailable(ArtifactStorageError):
    """The configured object store could not complete a required operation."""


class ArtifactStorageIntegrityError(ArtifactStorageError):
    """Stored bytes or version identity did not match the registry claim."""


class ArtifactStorageNotFound(ArtifactStorageError):
    """The exact registry-bound object version does not exist."""


@dataclass(frozen=True)
class ArtifactStorageSettings:
    endpoint: str
    bucket: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> ArtifactStorageSettings:
        values = {
            "endpoint": os.environ.get(STORAGE_ENDPOINT_ENV, "").strip(),
            "bucket": os.environ.get(STORAGE_BUCKET_ENV, "").strip(),
            "access_key": os.environ.get(STORAGE_ACCESS_KEY_ENV, "").strip(),
            "secret_key": os.environ.get(STORAGE_SECRET_KEY_ENV, "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ArtifactStorageUnavailable(
                "artifact storage configuration is incomplete: " + ", ".join(sorted(missing)),
            )
        return cls(**values)


@dataclass(frozen=True)
class StoredArtifactObject:
    object_key: str
    version_id: str
    content_digest: str


class ArtifactObjectStore(Protocol):
    def ensure_content(self, content_digest: str, body: bytes) -> StoredArtifactObject:
        """Return an exact immutable object binding for content-addressed bytes."""

    def read_version(self, object_key: str, version_id: str) -> bytes:
        """Read the exact immutable object version recorded by the registry."""


def delivery_art_object_key(content_digest: str) -> str:
    prefix = "sha256:"
    if not content_digest.startswith(prefix) or len(content_digest) != len(prefix) + 64:
        raise ValueError("content_digest must be a sha256 digest")
    digest_hex = content_digest.removeprefix(prefix)
    if any(character not in "0123456789abcdef" for character in digest_hex):
        raise ValueError("content_digest must use lowercase hexadecimal")
    return f"delivery-art/sha256/{digest_hex}.json"


class S3ArtifactObjectStore:
    """Small SigV4 client restricted to exact-version GET and non-delete PUT."""

    def __init__(self, settings: ArtifactStorageSettings, *, timeout_seconds: int = 20) -> None:
        endpoint = settings.endpoint.rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ArtifactStorageUnavailable("artifact storage endpoint must be HTTP or HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ArtifactStorageUnavailable(
                "artifact storage endpoint must not embed credentials",
            )
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ArtifactStorageUnavailable("artifact storage endpoint must not include a path or query")
        self._endpoint = endpoint
        self._bucket = settings.bucket
        self._access_key = settings.access_key
        self._secret_key = settings.secret_key
        self._host = parsed.netloc
        self._timeout_seconds = timeout_seconds

    def ensure_content(self, content_digest: str, body: bytes) -> StoredArtifactObject:
        if sha256_digest(body) != content_digest:
            raise ArtifactStorageIntegrityError("artifact bytes do not match their content digest")
        object_key = delivery_art_object_key(content_digest)
        try:
            current_body, current_version = self._get(object_key)
        except ArtifactStorageNotFound:
            version_id = self._put(object_key, body)
            persisted_body, persisted_version = self._get(object_key, version_id=version_id)
            if persisted_version != version_id or persisted_body != body:
                raise ArtifactStorageIntegrityError(
                    "artifact storage acknowledgement did not resolve the accepted bytes",
                )
            return StoredArtifactObject(object_key, version_id, content_digest)

        if sha256_digest(current_body) != content_digest:
            raise ArtifactStorageIntegrityError(
                "content-addressed object key resolves bytes with another digest",
            )
        self._require_version_id(current_version)
        return StoredArtifactObject(object_key, current_version, content_digest)

    def read_version(self, object_key: str, version_id: str) -> bytes:
        self._require_version_id(version_id)
        body, response_version = self._get(object_key, version_id=version_id)
        if response_version != version_id:
            raise ArtifactStorageIntegrityError(
                "version-qualified object read returned a different version",
            )
        return body

    @staticmethod
    def _sign(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    @staticmethod
    def _quote(value: str) -> str:
        return quote(value, safe="-_.~")

    def _request(
        self,
        method: str,
        object_key: str,
        *,
        body: bytes = b"",
        version_id: str | None = None,
    ):
        canonical_uri = f"/{self._quote(self._bucket)}/{quote(object_key, safe='/-_.~')}"
        canonical_query = "" if version_id is None else f"versionId={self._quote(version_id)}"
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        canonical_headers = (
            f"host:{self._host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        scope = f"{date_stamp}/us-east-1/s3/aws4_request"
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                payload_hash,
            ],
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ],
        )
        date_key = self._sign(("AWS4" + self._secret_key).encode("utf-8"), date_stamp)
        region_key = self._sign(date_key, "us-east-1")
        service_key = self._sign(region_key, "s3")
        signing_key = self._sign(service_key, "aws4_request")
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self._access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        url = f"{self._endpoint}{canonical_uri}"
        if canonical_query:
            url = f"{url}?{canonical_query}"
        request = Request(
            url,
            data=body if method == "PUT" else None,
            headers={
                "Authorization": authorization,
                "Host": self._host,
                "x-amz-content-sha256": payload_hash,
                "x-amz-date": amz_date,
            },
            method=method,
        )
        try:
            return urlopen(request, timeout=self._timeout_seconds)
        except HTTPError as exc:
            if exc.code == 404:
                raise ArtifactStorageNotFound("artifact object was not found") from exc
            raise ArtifactStorageUnavailable(
                f"artifact storage returned HTTP {exc.code}",
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise ArtifactStorageUnavailable("artifact storage request failed") from exc

    def _get(self, object_key: str, *, version_id: str | None = None) -> tuple[bytes, str]:
        with self._request("GET", object_key, version_id=version_id) as response:
            body = response.read()
            response_version = response.headers.get("x-amz-version-id", "")
        self._require_version_id(response_version)
        return body, response_version

    def _put(self, object_key: str, body: bytes) -> str:
        with self._request("PUT", object_key, body=body) as response:
            response.read()
            version_id = response.headers.get("x-amz-version-id", "")
        self._require_version_id(version_id)
        return version_id

    @staticmethod
    def _require_version_id(version_id: str) -> None:
        if not version_id or version_id == "null":
            raise ArtifactStorageIntegrityError(
                "artifact storage did not return an immutable object version ID",
            )
