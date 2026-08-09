#!/usr/bin/env python3
"""Verify version-bound WGCF evidence with the application storage identity."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


def _quote(value: str) -> str:
    return quote(value, safe="-_.~")


class S3Client:
    def __init__(self, *, access_key: str | None = None, secret_key: str | None = None) -> None:
        self.endpoint = os.environ["WGCF_EVIDENCE_STORAGE_ENDPOINT"].rstrip("/")
        self.bucket = os.environ["WGCF_EVIDENCE_STORAGE_BUCKET"]
        self.access_key = access_key or os.environ["WGCF_EVIDENCE_STORAGE_ACCESS_KEY"]
        self.secret_key = secret_key or os.environ["WGCF_EVIDENCE_STORAGE_SECRET_KEY"]
        parsed = urlsplit(self.endpoint)
        self.host = parsed.netloc

    @staticmethod
    def _sign(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode(), hashlib.sha256).digest()

    def request(
        self,
        method: str,
        object_key: str,
        *,
        body: bytes = b"",
        version_id: str | None = None,
    ):
        canonical_uri = f"/{_quote(self.bucket)}/{quote(object_key, safe='/-_.~')}"
        canonical_query = ""
        if version_id is not None:
            canonical_query = f"versionId={_quote(version_id)}"
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        canonical_headers = (
            f"host:{self.host}\n"
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
            ]
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        date_key = self._sign(("AWS4" + self.secret_key).encode(), date_stamp)
        region_key = self._sign(date_key, "us-east-1")
        service_key = self._sign(region_key, "s3")
        signing_key = self._sign(service_key, "aws4_request")
        signature = hmac.new(
            signing_key,
            string_to_sign.encode(),
            hashlib.sha256,
        ).hexdigest()
        authorization = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        url = f"{self.endpoint}{canonical_uri}"
        if canonical_query:
            url = f"{url}?{canonical_query}"
        return urlopen(
            Request(
                url,
                data=body if method == "PUT" else None,
                headers={
                    "Authorization": authorization,
                    "Host": self.host,
                    "x-amz-content-sha256": payload_hash,
                    "x-amz-date": amz_date,
                },
                method=method,
            ),
            timeout=20,
        )

    def get(self, object_key: str, *, version_id: str | None = None) -> tuple[bytes, str]:
        with self.request("GET", object_key, version_id=version_id) as response:
            body = response.read()
            response_version_id = response.headers.get("x-amz-version-id", "")
        return body, response_version_id

    def put(self, object_key: str, body: bytes) -> str:
        with self.request("PUT", object_key, body=body) as response:
            response.read()
            version_id = response.headers.get("x-amz-version-id", "")
        return version_id

    def delete_is_denied(
        self,
        object_key: str,
        *,
        version_id: str | None = None,
    ) -> bool:
        try:
            with self.request("DELETE", object_key, version_id=version_id) as response:
                response.read()
        except HTTPError as error:
            return error.code == 403
        return False


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _require_version_id(version_id: str, label: str) -> str:
    if not version_id or version_id == "null":
        raise SystemExit(f"{label} did not return an object version ID")
    return version_id


def preserve_overwrite(
    client: S3Client,
    object_key: str,
    expected_digest: str,
    accepted_version_id: str | None = None,
) -> dict:
    requested_version_id = accepted_version_id or None
    accepted_body, response_version_id = client.get(
        object_key,
        version_id=requested_version_id,
    )
    accepted_version_id = response_version_id
    if _digest(accepted_body) != expected_digest:
        raise SystemExit("accepted evidence digest does not match the profile contract")
    if requested_version_id is not None and accepted_version_id != requested_version_id:
        raise SystemExit("existing receipt resolved a different accepted object version")
    accepted_version_materialized = not accepted_version_id or accepted_version_id == "null"
    if accepted_version_materialized:
        accepted_version_id = client.put(object_key, accepted_body)
        _require_version_id(accepted_version_id, "accepted evidence version migration")
        migrated_body, migrated_version_id = client.get(object_key)
        if _digest(migrated_body) != expected_digest:
            raise SystemExit("version migration changed the accepted evidence bytes")
        if migrated_version_id != accepted_version_id:
            raise SystemExit("version migration did not become the current object")
    else:
        _require_version_id(accepted_version_id, "accepted evidence read")

    overwrite_body = json.dumps(
        {
            "accepted_sha256": expected_digest,
            "proof": "same-key-overwrite-preservation",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    overwrite_digest = _digest(overwrite_body)
    overwritten = False
    overwrite_version_id = ""
    restored_version_id = ""
    try:
        overwritten = True
        overwrite_version_id = client.put(object_key, overwrite_body)
        _require_version_id(overwrite_version_id, "same-key overwrite")
        if overwrite_version_id == accepted_version_id:
            raise SystemExit("same-key overwrite reused the accepted object version ID")

        current_body, current_version_id = client.get(object_key)
        if _digest(current_body) != overwrite_digest:
            raise SystemExit("same-key overwrite did not become the current object")
        if current_version_id != overwrite_version_id:
            raise SystemExit("current object does not bind the overwrite version ID")

        preserved_body, preserved_version_id = client.get(
            object_key,
            version_id=accepted_version_id,
        )
        if _digest(preserved_body) != expected_digest:
            raise SystemExit("accepted evidence bytes were not preserved after overwrite")
        if preserved_version_id != accepted_version_id:
            raise SystemExit("version-qualified read returned a different object version")
    finally:
        if overwritten:
            restored_version_id = client.put(object_key, accepted_body)

    _require_version_id(restored_version_id, "accepted evidence restore")
    restored_body, current_version_id = client.get(object_key)
    if _digest(restored_body) != expected_digest:
        raise SystemExit("accepted evidence was not restored as the current object")
    if current_version_id != restored_version_id:
        raise SystemExit("current object does not bind the restored version ID")

    return {
        "bucket": client.bucket,
        "object_key": object_key,
        "accepted_version_id": accepted_version_id,
        "accepted_version_materialized": accepted_version_materialized,
        "accepted_sha256": expected_digest,
        "overwrite_version_id": overwrite_version_id,
        "overwrite_sha256": overwrite_digest,
        "restored_version_id": restored_version_id,
        "same_key_overwrite_proved": True,
        "accepted_version_preserved": True,
        "current_object_restored": True,
        "application_credential_version_read": True,
        "root_credential_absent": True,
    }


def verify(
    client: S3Client,
    object_key: str,
    expected_digest: str,
    accepted_version_id: str,
) -> dict:
    _require_version_id(accepted_version_id, "receipt-bound evidence")
    accepted_body, response_version_id = client.get(
        object_key,
        version_id=accepted_version_id,
    )
    if _digest(accepted_body) != expected_digest:
        raise SystemExit("receipt-bound evidence digest does not match the profile contract")
    if response_version_id != accepted_version_id:
        raise SystemExit("receipt-bound evidence read returned a different object version")

    current_body, current_version_id = client.get(object_key)
    current_digest = _digest(current_body)
    if not client.delete_is_denied(object_key):
        raise SystemExit("application storage credential unexpectedly permits object deletion")
    if not client.delete_is_denied(object_key, version_id=accepted_version_id):
        raise SystemExit(
            "application storage credential unexpectedly permits receipt-bound version deletion"
        )

    return {
        "bucket": client.bucket,
        "object_key": object_key,
        "accepted_version_id": accepted_version_id,
        "accepted_sha256": expected_digest,
        "current_version_id": current_version_id,
        "current_sha256": current_digest,
        "current_matches_accepted": current_digest == expected_digest,
        "application_credential_read": True,
        "application_credential_version_read": True,
        "application_credential_delete_denied": True,
        "application_credential_version_delete_denied": True,
        "root_credential_absent": True,
    }


def probe_receipt_version(
    client: S3Client,
    object_key: str,
    accepted_version_id: str,
) -> dict:
    _require_version_id(accepted_version_id, "receipt-bound evidence")
    try:
        _, response_version_id = client.get(
            object_key,
            version_id=accepted_version_id,
        )
    except HTTPError as error:
        if error.code == 404:
            return {
                "bucket": client.bucket,
                "object_key": object_key,
                "object_version_id": accepted_version_id,
                "state": "missing",
            }
        raise
    if response_version_id != accepted_version_id:
        raise SystemExit("receipt-bound evidence probe returned a different object version")
    return {
        "bucket": client.bucket,
        "object_key": object_key,
        "object_version_id": accepted_version_id,
        "state": "present",
    }


def _safe_package_path(package_root: Path, relative_path: str) -> Path:
    candidate = (package_root / relative_path).resolve()
    if package_root != candidate and package_root not in candidate.parents:
        raise SystemExit(f"backup package path escapes its root: {relative_path}")
    if not candidate.is_file():
        raise SystemExit(f"backup package member is missing: {relative_path}")
    return candidate


def _storage_ref(profile_id: str, bucket: str, object_key: str, version_id: str) -> str:
    return (
        f"wgcf-storage://{profile_id}/{bucket}/{object_key}"
        f"?versionId={_quote(version_id)}"
    )


def normalize_version_preservation(version_preservation: object, receipt_name: str) -> dict:
    if (
        not isinstance(version_preservation, dict)
        or version_preservation.get("accepted_version_preserved") is not True
    ):
        raise SystemExit(f"restore receipt has invalid version preservation: {receipt_name}")
    if version_preservation.get("same_key_overwrite_proved") is True:
        expected_version_keys = {
            "accepted_version_preserved",
            "same_key_overwrite_proved",
            "overwrite_version_id",
            "restored_version_id",
        }
        required_version_fields = ("overwrite_version_id", "restored_version_id")
    elif version_preservation.get("restore_rebound") is True:
        expected_version_keys = {
            "accepted_version_preserved",
            "restore_rebound",
            "rebound_object_version_id",
            "restored_current_version_id",
        }
        required_version_fields = (
            "rebound_object_version_id",
            "restored_current_version_id",
        )
    else:
        raise SystemExit(f"restore receipt has unsupported version proof: {receipt_name}")
    if set(version_preservation) != expected_version_keys:
        raise SystemExit(f"restore receipt has unsupported version claims: {receipt_name}")
    if not all(
        isinstance(version_preservation.get(field), str)
        and version_preservation[field]
        for field in required_version_fields
    ):
        raise SystemExit(f"restore receipt has incomplete version proof: {receipt_name}")
    return {key: version_preservation[key] for key in sorted(expected_version_keys)}


def normalize_restore_supersession(
    claim: object,
    *,
    active_scope: dict[str, str],
    object_key: str,
    current_version_id: str,
    restored_current_version_id: str,
    content_digest: str,
    current_storage_ref: str,
    receipt_name: str,
) -> dict:
    expected_keys = {
        "prior_object_version_id",
        "prior_storage_ref",
        "rebound_object_version_id",
        "rebound_storage_ref",
        "restored_current_version_id",
        "content_sha256",
        "superseded_at",
    }
    if not isinstance(claim, dict) or set(claim) != expected_keys:
        raise SystemExit(f"restore receipt has unsupported supersession claims: {receipt_name}")
    if not all(isinstance(claim.get(key), str) and claim[key] for key in expected_keys):
        raise SystemExit(f"restore receipt has incomplete supersession proof: {receipt_name}")
    expected_prior_ref = _storage_ref(
        active_scope["profile_id"],
        active_scope["bucket"],
        object_key,
        claim["prior_object_version_id"],
    )
    if (
        claim["prior_storage_ref"] != expected_prior_ref
        or claim["rebound_object_version_id"] != current_version_id
        or claim["rebound_storage_ref"] != current_storage_ref
        or claim["restored_current_version_id"] != restored_current_version_id
        or claim["content_sha256"] != content_digest
    ):
        raise SystemExit(f"restore receipt has invalid supersession binding: {receipt_name}")
    return {key: claim[key] for key in sorted(expected_keys)}


def validate_storage_receipt(
    receipt: dict,
    *,
    active_scope: dict[str, str],
    object_key: str,
    version_id: str,
    content_digest: str,
    receipt_name: str,
) -> dict:
    expected = {
        "schema_version": 2,
        "receipt_type": "dev-integration-storage",
        "profile_id": active_scope["profile_id"],
        "kubernetes_namespace": active_scope["kubernetes_namespace"],
        "bucket": active_scope["bucket"],
        "object_key": object_key,
        "object_version_id": version_id,
        "content_sha256": content_digest,
        "storage_ref": _storage_ref(
            active_scope["profile_id"],
            active_scope["bucket"],
            object_key,
            version_id,
        ),
        "service_identity_ref": active_scope["service_identity_ref"],
        "application_secret_ref": active_scope["application_secret_ref"],
        "root_credential_exposed_to_api": False,
        "oos_credential_issued": False,
        "openproject_credential_issued": False,
        "network_exposure": "namespace-local-network-policy",
        "object_versioning": "enabled",
        "transport_encryption": "not-governed-dev-integration-http",
        "at_rest_encryption": "not-governed-local-path-pvc",
        "governed_stage_or_prod_claim": False,
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise SystemExit(f"restore receipt has invalid fixed claims: {receipt_name}")
    if receipt.get("network_enforcement") != {
        "api_allowed": True,
        "maintenance_allowed": True,
        "unselected_pod_denied": True,
        "label_selector_is_workload_identity": False,
    }:
        raise SystemExit(f"restore receipt has invalid network claims: {receipt_name}")
    for field in ("credential_isolation_verified_at", "verified_at"):
        if not isinstance(receipt.get(field), str) or not receipt[field]:
            raise SystemExit(f"restore receipt has invalid {field}: {receipt_name}")
    version_preservation = normalize_version_preservation(
        receipt.get("version_preservation"),
        receipt_name,
    )
    rotation = receipt.get("credential_rotation")
    if rotation is not None:
        denial_fields = (
            "retired_root_credential_authentication_denied",
            "retired_application_credential_authentication_denied",
        )
        if (
            not isinstance(rotation, dict)
            or rotation.get("rotation_detected") is not True
            or any(rotation.get(field) not in {None, True} for field in denial_fields)
            or not any(rotation.get(field) is True for field in denial_fields)
        ):
            raise SystemExit(f"restore receipt has invalid credential rotation: {receipt_name}")
    return version_preservation


def assert_credentials_denied(client: S3Client, object_key: str) -> dict:
    try:
        client.get(object_key)
    except HTTPError as error:
        if error.code in {401, 403}:
            return {
                "bucket": client.bucket,
                "object_key": object_key,
                "authentication_denied": True,
            }
        raise SystemExit(
            f"retired credential returned unexpected HTTP status {error.code}"
        ) from error
    raise SystemExit("retired storage credential still authenticates")


def rebind_receipts(
    client: S3Client,
    package_root: Path,
    manifest: dict,
    active_scope: dict[str, str],
) -> dict:
    package_root = package_root.resolve()
    bindings = manifest.get("receipt_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise SystemExit("restore manifest has no receipt-bound evidence")
    required_scope = {
        "profile_id",
        "kubernetes_namespace",
        "bucket",
        "service_identity_ref",
        "application_secret_ref",
    }
    if set(active_scope) != required_scope or not all(
        isinstance(active_scope[key], str) and active_scope[key]
        for key in required_scope
    ):
        raise SystemExit("active restore scope is incomplete")
    if active_scope["bucket"] != client.bucket:
        raise SystemExit("active restore bucket does not match the storage client")
    for field in ("profile_id", "kubernetes_namespace", "bucket"):
        if manifest.get(field) != active_scope[field]:
            raise SystemExit(
                f"restore manifest {field} does not match the active storage scope"
            )

    rebound_root = package_root / "rebound-receipts"
    rebound_root.mkdir(parents=True, exist_ok=True)
    mappings = []
    for binding in bindings:
        receipt_name = binding.get("receipt_name")
        object_key = binding.get("object_key")
        expected_digest = binding.get("content_sha256")
        prior_version_id = binding.get("prior_object_version_id")
        if not all(
            isinstance(value, str) and value
            for value in (receipt_name, object_key, expected_digest, prior_version_id)
        ):
            raise SystemExit("restore manifest contains an invalid receipt binding")
        if "/" in receipt_name or receipt_name in {".", ".."}:
            raise SystemExit("restore manifest contains an unsafe receipt name")
        body_path = _safe_package_path(package_root, binding["body_archive_path"])
        receipt_path = _safe_package_path(package_root, binding["receipt_archive_path"])
        current_path = _safe_package_path(package_root, binding["current_archive_path"])
        bound_body = body_path.read_bytes()
        current_body = current_path.read_bytes()
        if _digest(bound_body) != expected_digest:
            raise SystemExit(f"receipt-bound backup digest mismatch: {receipt_name}")

        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        expected_prior_ref = _storage_ref(
            active_scope["profile_id"],
            active_scope["bucket"],
            object_key,
            prior_version_id,
        )
        if binding.get("prior_storage_ref") != expected_prior_ref:
            raise SystemExit(f"restore manifest prior reference is invalid: {receipt_name}")
        prior_version_preservation = validate_storage_receipt(
            receipt,
            active_scope=active_scope,
            object_key=object_key,
            version_id=prior_version_id,
            content_digest=expected_digest,
            receipt_name=receipt_name,
        )

        rebound_version_id = _require_version_id(
            client.put(object_key, bound_body),
            f"receipt rebinding for {receipt_name}",
        )
        rebound_body, returned_version_id = client.get(
            object_key,
            version_id=rebound_version_id,
        )
        if returned_version_id != rebound_version_id or _digest(rebound_body) != expected_digest:
            raise SystemExit(f"receipt rebinding verification failed: {receipt_name}")

        current_version_id = _require_version_id(
            client.put(object_key, current_body),
            f"current object restore for {receipt_name}",
        )
        restored_current, returned_current_version_id = client.get(object_key)
        if (
            returned_current_version_id != current_version_id
            or restored_current != current_body
        ):
            raise SystemExit(f"current object restore verification failed: {receipt_name}")

        rebound_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00",
            "Z",
        )
        prior_storage_ref = receipt.get("storage_ref")
        new_storage_ref = _storage_ref(
            active_scope["profile_id"],
            active_scope["bucket"],
            object_key,
            rebound_version_id,
        )
        receipt = {
            "schema_version": 2,
            "receipt_type": "dev-integration-storage",
            "profile_id": active_scope["profile_id"],
            "kubernetes_namespace": active_scope["kubernetes_namespace"],
            "bucket": active_scope["bucket"],
            "object_key": object_key,
            "object_version_id": rebound_version_id,
            "content_sha256": expected_digest,
            "storage_ref": new_storage_ref,
            "service_identity_ref": active_scope["service_identity_ref"],
            "application_secret_ref": active_scope["application_secret_ref"],
            "root_credential_exposed_to_api": False,
            "oos_credential_issued": False,
            "openproject_credential_issued": False,
            "network_exposure": "namespace-local-network-policy",
            "credential_isolation_verified_at": receipt[
                "credential_isolation_verified_at"
            ],
            "network_enforcement": {
                "api_allowed": True,
                "maintenance_allowed": True,
                "unselected_pod_denied": True,
                "label_selector_is_workload_identity": False,
            },
            "object_versioning": "enabled",
            "pre_restore_version_preservation": prior_version_preservation,
            "version_preservation": {
                "accepted_version_preserved": True,
                "restore_rebound": True,
                "rebound_object_version_id": rebound_version_id,
                "restored_current_version_id": current_version_id,
            },
            "transport_encryption": "not-governed-dev-integration-http",
            "at_rest_encryption": "not-governed-local-path-pvc",
            "governed_stage_or_prod_claim": False,
            "verified_at": rebound_at,
        }
        receipt["restore_supersession"] = normalize_restore_supersession(
            {
                "prior_object_version_id": prior_version_id,
                "prior_storage_ref": prior_storage_ref,
                "rebound_object_version_id": rebound_version_id,
                "rebound_storage_ref": new_storage_ref,
                "restored_current_version_id": current_version_id,
                "content_sha256": expected_digest,
                "superseded_at": rebound_at,
            },
            active_scope=active_scope,
            object_key=object_key,
            current_version_id=rebound_version_id,
            restored_current_version_id=current_version_id,
            content_digest=expected_digest,
            current_storage_ref=new_storage_ref,
            receipt_name=receipt_name,
        )
        rebound_path = rebound_root / f"{receipt_name}.json"
        rebound_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        mappings.append(receipt["restore_supersession"] | {"receipt_name": receipt_name})

    return {
        "schema_version": 1,
        "bucket": client.bucket,
        "version_ids_preserved": False,
        "receipt_identity_rebound": True,
        "receipt_rebindings": mappings,
    }


def main() -> int:
    if len(sys.argv) not in {3, 4, 5}:
        raise SystemExit(
            "usage: verify_storage_versioning.py "
            "preserve-overwrite|verify EXPECTED_SHA256 OBJECT_KEY [VERSION_ID], or "
            "expect-denied|expect-denied-stdin OBJECT_KEY, or "
            "probe-version OBJECT_KEY VERSION_ID, or "
            "rebind PACKAGE_ROOT MANIFEST_PATH OUTPUT_PATH"
        )
    mode = sys.argv[1]
    if mode == "expect-denied" and len(sys.argv) == 3:
        result = assert_credentials_denied(S3Client(), sys.argv[2])
        print(json.dumps(result, sort_keys=True))
        return 0
    if mode == "expect-denied-stdin" and len(sys.argv) == 3:
        access_key = sys.stdin.readline().rstrip("\n")
        secret_key = sys.stdin.readline().rstrip("\n")
        if not access_key or not secret_key:
            raise SystemExit("retired credential input is incomplete")
        result = assert_credentials_denied(
            S3Client(access_key=access_key, secret_key=secret_key),
            sys.argv[2],
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    if mode == "rebind" and len(sys.argv) == 5:
        if "MINIO_ROOT_USER" in os.environ or "MINIO_ROOT_PASSWORD" in os.environ:
            raise SystemExit("root storage credentials must not be exposed to the verifier")
        package_root = Path(sys.argv[2])
        manifest = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
        client = S3Client()
        result = rebind_receipts(
            client,
            package_root,
            manifest,
            {
                "profile_id": os.environ["WGCF_EVIDENCE_PROFILE_ID"],
                "kubernetes_namespace": os.environ[
                    "WGCF_EVIDENCE_KUBERNETES_NAMESPACE"
                ],
                "bucket": client.bucket,
                "service_identity_ref": os.environ[
                    "WGCF_EVIDENCE_SERVICE_IDENTITY_REF"
                ],
                "application_secret_ref": os.environ[
                    "WGCF_EVIDENCE_APPLICATION_SECRET_REF"
                ],
            },
        )
        Path(sys.argv[4]).write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0
    if mode == "probe-version" and len(sys.argv) == 4:
        if "MINIO_ROOT_USER" in os.environ or "MINIO_ROOT_PASSWORD" in os.environ:
            raise SystemExit("root storage credentials must not be exposed to the verifier")
        result = probe_receipt_version(S3Client(), sys.argv[2], sys.argv[3])
        print(result["state"])
        return 0
    expected_digest = sys.argv[2]
    object_key = sys.argv[3]
    if len(expected_digest) != 64:
        raise SystemExit("expected SHA-256 must contain 64 hexadecimal characters")
    if "MINIO_ROOT_USER" in os.environ or "MINIO_ROOT_PASSWORD" in os.environ:
        raise SystemExit("root storage credentials must not be exposed to the verifier")

    client = S3Client()
    if mode == "preserve-overwrite" and len(sys.argv) in {4, 5}:
        result = preserve_overwrite(
            client,
            object_key,
            expected_digest,
            sys.argv[4] if len(sys.argv) == 5 else None,
        )
    elif mode == "verify" and len(sys.argv) == 5:
        result = verify(client, object_key, expected_digest, sys.argv[4])
    else:
        raise SystemExit("invalid storage version verification mode or arguments")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
