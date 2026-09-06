"""Non-mutating readiness policy for intake-to-inventory promotion."""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from .workspace_inventory_contracts import (
    COLLECTIONS,
    InventoryContracts,
    InventoryRequestError,
    InventorySnapshot,
    digest,
)


def evaluate_inventory_promotion(
    envelope: dict[str, Any],
    snapshot: InventorySnapshot,
    contracts: InventoryContracts,
    evaluated_at: datetime,
) -> dict[str, Any]:
    request = envelope["request"]
    target = request["target"]
    kind, name = target["kind"], target["name"]
    collection = COLLECTIONS[kind]
    register = snapshot.records["intake-register"]
    inventory = snapshot.records[collection]
    intake_entry = register[collection].get(name)
    if intake_entry is None:
        raise InventoryRequestError(
            "promotion target is absent from committed Workspace Intake; refresh preparation"
        )

    active_record = _inventory_record(inventory, kind, name)
    if active_record is not None:
        raise InventoryRequestError(
            "promotion target already exists in active inventory; use a lifecycle operation"
        )
    observed_state = {
        "intake_register_digest": digest(register),
        "active_inventory_digest": digest(inventory),
        "intake_entry_version": intake_entry["record"]["version"],
        "intake_entry_digest": digest(intake_entry),
        "active_record_version": None,
        "active_record_digest": None,
    }
    findings: list[dict[str, str]] = []
    stale = False

    def find(code: str, message: str, *, is_stale: bool = False) -> None:
        nonlocal stale
        stale = stale or is_stale
        findings.append({"code": code, "severity": "blocking", "message": message})

    if envelope["authority_revision"] != snapshot.revision:
        find(
            "stale-authority",
            "Refresh the promotion against the current merged Workspace Governance revision.",
            is_stale=True,
        )
    if target["record_id"] != f"{kind}:{name}":
        find("target-mismatch", "The promotion target identity must match its kind and name.")
    if request["active_record"]["kind"] != kind or request["active_record"]["id"] != target["record_id"]:
        find("active-record-target-mismatch", "The active record must bind the exact promotion target.")
    if request["intake_entry_ref"] != {
        "id": intake_entry["record"]["id"],
        "version": observed_state["intake_entry_version"],
        "digest": observed_state["intake_entry_digest"],
    }:
        find(
            "stale-intake-entry",
            "Refresh the promotion against the exact admitted intake entry.",
            is_stale=True,
        )
    if request["expected_state"] != observed_state:
        find(
            "stale-inventory-state",
            "Refresh the promotion against current intake and active inventory digests.",
            is_stale=True,
        )
    if intake_entry["status"] != "admitted":
        find("intake-entry-not-admitted", "Only an admitted Workspace Intake entry can be promoted.")
    requested_at = datetime.fromisoformat(request["requested_at"].replace("Z", "+00:00"))
    if requested_at > evaluated_at:
        find("future-request", "The promotion request timestamp is ahead of the evaluation clock.")

    value = request["active_record"]["value"]
    if kind == "product":
        if value.get("lifecycle") != value.get("maturity"):
            find("compatibility-alias-mismatch", "Product lifecycle must equal product maturity.")
    elif value.get("lifecycle") != value.get("posture"):
        find("compatibility-alias-mismatch", f"{kind.title()} lifecycle must equal posture.")

    try:
        _validate_active_value(request, contracts)
    except InventoryRequestError as exc:
        find("active-record-invalid", str(exc))

    for existing in _all_inventory_records(snapshot):
        mutation = existing.get("record", {}).get("last_mutation", {})
        if mutation.get("idempotency_key") == request["idempotency_key"]:
            find("idempotency-conflict", "The idempotency key already identifies another mutation.")
            break
        if mutation.get("request_ref") == request["request_id"]:
            find("request-identity-conflict", "The request identity already belongs to another mutation.")
            break

    return {
        "observed_state": observed_state,
        "outcome": "stale" if stale else ("blocked" if findings else "ready"),
        "findings": findings,
    }


def _inventory_record(inventory: dict[str, Any], kind: str, name: str) -> dict[str, Any] | None:
    record = inventory.get(COLLECTIONS[kind], {}).get(name)
    if record is None and kind == "repo":
        record = inventory.get("retired_repos", {}).get(name)
    return record


def _all_inventory_records(snapshot: InventorySnapshot) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for kind, collection in COLLECTIONS.items():
        inventory = snapshot.records[collection]
        records.extend(inventory.get(collection, {}).values())
        if kind == "repo":
            records.extend(inventory.get("retired_repos", {}).values())
    return records


def _validate_active_value(request: dict[str, Any], contracts: InventoryContracts) -> None:
    target = request["target"]
    kind, name = target["kind"], target["name"]
    collection = COLLECTIONS[kind]
    value = copy.deepcopy(request["active_record"]["value"])
    value["record"] = {
        "id": target["record_id"],
        "version": 1,
        "lineage": {
            "source": "workspace-intake",
            "source_ref": request["intake_entry_ref"]["id"],
            "source_digest": request["intake_entry_ref"]["digest"],
            "intake_entry_version": request["intake_entry_ref"]["version"],
        },
        "last_mutation": {
            "id": f"workspace-inventory-mutation:{request['idempotency_key']}",
            "action": "promote",
            "idempotency_key": request["idempotency_key"],
            "request_ref": request["request_id"],
            "request_digest": request["request_digest"],
            "readiness_ref": f"workspace-inventory-readiness:{request['request_id']}",
            "readiness_digest": request["request_digest"],
            "applied_at": request["requested_at"],
        },
    }
    candidate: dict[str, Any] = {"schema_version": 2, collection: {name: value}}
    if kind == "repo":
        candidate["retired_repos"] = {}
    contracts.validate(f"{collection}.schema.json", candidate)
