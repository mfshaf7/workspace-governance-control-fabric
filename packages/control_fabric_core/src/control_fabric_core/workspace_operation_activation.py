from __future__ import annotations

from hashlib import sha256
from typing import Any, Mapping

from jsonschema import Draft202012Validator
import yaml


OPERATION_PATH = "contracts/workspace-intake-inventory-operation.yaml"
OPERATION_SCHEMA_PATH = (
    "contracts/schemas/workspace-intake-inventory-operation.schema.json"
)
OPERATION_SCHEMA_NAME = "workspace-intake-inventory-operation.schema.json"
EXPECTED_CAPABILITIES = {
    "workspace-intake-classification",
    "workspace-active-inventory-promotion",
    "workspace-active-inventory-lifecycle",
}
CAPABILITY_DOCUMENTS = {
    "contracts/workspace-intake.yaml": "workspace-intake.schema.json",
    "contracts/workspace-active-inventory.yaml": "workspace-active-inventory.schema.json",
    "contracts/workspace-inventory-lifecycle.yaml": (
        "workspace-inventory-lifecycle.schema.json"
    ),
}


def validate_workspace_operation_activation(
    manifest: Mapping[str, Any],
    files: Mapping[str, bytes],
    validators: Mapping[str, Draft202012Validator],
) -> dict[str, Any]:
    activation = manifest["activation_contract"]
    required = {
        "repo",
        "commit",
        "path",
        "schema_path",
        "contract_work_ref",
        "architecture_packet_ref",
        "content_sha256",
        "schema_sha256",
    }
    if not isinstance(activation, dict) or set(activation) != required:
        raise ValueError("invalid workspace operation activation binding")
    if (
        manifest["runtime_activation"] is not True
        or activation["repo"] != "workspace-governance"
        or activation["commit"] != manifest["authority_commit"]
        or activation["path"] != OPERATION_PATH
        or activation["schema_path"] != OPERATION_SCHEMA_PATH
        or activation["contract_work_ref"] != "openproject://work_packages/1206"
        or activation["architecture_packet_ref"]
        != "architecture-packet:delivery-1203-v1"
    ):
        raise ValueError("workspace operation activation authority is invalid")

    operation_raw = files[OPERATION_PATH]
    schema_raw = files[OPERATION_SCHEMA_PATH]
    if (
        activation["content_sha256"] != sha256(operation_raw).hexdigest()
        or activation["schema_sha256"] != sha256(schema_raw).hexdigest()
    ):
        raise ValueError("workspace operation activation digest mismatch")

    operation = yaml.safe_load(operation_raw)
    if not isinstance(operation, dict):
        raise ValueError("workspace operation activation contract is invalid")
    errors = sorted(
        validators[OPERATION_SCHEMA_NAME].iter_errors(operation),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise ValueError(f"workspace operation activation contract {path}: {error.message}")
    if (
        set(operation["applies_to"]) != EXPECTED_CAPABILITIES
        or operation["architecture"]["contract_work_ref"]
        != activation["contract_work_ref"]
        or operation["architecture"]["architecture_packet_ref"]
        != activation["architecture_packet_ref"]
    ):
        raise ValueError("workspace operation activation scope is invalid")

    for document_path, schema_name in CAPABILITY_DOCUMENTS.items():
        if document_path not in files:
            continue
        document = yaml.safe_load(files[document_path])
        if not isinstance(document, dict):
            raise ValueError(f"workspace operation document is invalid: {document_path}")
        document_errors = sorted(
            validators[schema_name].iter_errors(document),
            key=lambda error: list(error.absolute_path),
        )
        if document_errors:
            error = document_errors[0]
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            raise ValueError(
                f"workspace operation document {document_path} {path}: {error.message}"
            )
    return operation
