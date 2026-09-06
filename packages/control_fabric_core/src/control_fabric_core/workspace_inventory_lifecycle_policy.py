"""Non-mutating readiness policy for Workspace Inventory lifecycle changes."""

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


TRANSITIONS = {
    "update": {"active": "active", "suspended": "suspended"},
    "suspend": {"active": "suspended"},
    "restore": {"suspended": "active", "retired": "active"},
    "retire": {"active": "retired", "suspended": "retired"},
}


def evaluate_inventory_lifecycle(
    envelope: dict[str, Any],
    snapshot: InventorySnapshot,
    contracts: InventoryContracts,
    evaluated_at: datetime,
) -> dict[str, Any]:
    request = envelope["request"]
    target = request["target"]
    kind, name = target["kind"], target["name"]
    collection = COLLECTIONS[kind]
    inventory = snapshot.records[collection]
    history = snapshot.records["workspace-inventory-history"]
    record = _inventory_record(inventory, kind, name)
    if record is None:
        raise InventoryRequestError(
            "lifecycle target is absent from committed active inventory"
        )

    observed_state = {
        "active_inventory_digest": digest(inventory),
        "history_digest": digest(history),
        "record_version": record["record"]["version"],
        "record_digest": digest(record),
        "posture": record["posture"],
    }
    findings: list[dict[str, str]] = []
    def find(code: str, message: str) -> None:
        findings.append({"code": code, "severity": "blocking", "message": message})

    if envelope["authority_revision"] != snapshot.revision:
        find(
            "stale-authority",
            "Refresh the lifecycle request against the current merged Workspace Governance revision.",
        )
    if target["record_id"] != f"{kind}:{name}":
        find("target-mismatch", "The lifecycle target identity must match its kind and name.")
    if request["expected_state"] != observed_state:
        find(
            "stale-inventory-state",
            "Refresh the lifecycle request against the current inventory, history, and record state.",
        )

    action = request["action"]
    current_posture = record["posture"]
    next_posture = TRANSITIONS.get(action, {}).get(current_posture)
    if next_posture is None:
        find(
            "illegal-lifecycle-transition",
            f"The {action} action is not allowed while posture is {current_posture}.",
        )

    requested_at = datetime.fromisoformat(request["requested_at"].replace("Z", "+00:00"))
    if requested_at > evaluated_at:
        find("future-request", "The lifecycle request timestamp is ahead of the evaluation clock.")

    if action == "update":
        requested_value = request["requested_value"]
        if "record" in requested_value:
            find("record-envelope-replacement", "Update cannot replace the inventory record envelope.")
        elif requested_value.get("posture") != current_posture:
            find("posture-change-through-update", "Update must preserve the current posture.")
        else:
            try:
                _validate_updated_value(
                    kind,
                    name,
                    requested_value,
                    record,
                    contracts,
                )
            except InventoryRequestError as exc:
                find("updated-record-invalid", str(exc))

    target_events = _target_history(history, target["record_id"])
    if target_events and target_events[-1]["after"] != {
        "record_version": observed_state["record_version"],
        "record_digest": observed_state["record_digest"],
        "posture": observed_state["posture"],
    }:
        find(
            "stale-history-head",
            "The latest lifecycle history event does not bind the current inventory record.",
        )
    if action == "restore":
        if not target_events:
            find(
                "restore-history-missing",
                "Restore requires a prior suspension or retirement event.",
            )
        else:
            latest = target_events[-1]
            expected_ref = {
                "id": latest["event_id"],
                "digest": latest["event_digest"],
            }
            if latest["action"] not in {"suspend", "retire"}:
                find(
                    "restore-source-invalid",
                    "Restore requires the latest lifecycle event to be suspension or retirement.",
                )
            if request["prior_event_ref"] != expected_ref:
                find(
                    "stale-restore-event",
                    "Refresh restore against the latest lifecycle history event.",
                )

    for event in history["events"]:
        if event["idempotency_key"] == request["idempotency_key"]:
            find(
                "idempotency-conflict",
                "The idempotency key already identifies a lifecycle event.",
            )
            break
        if event["request_ref"]["id"] == request["request_id"]:
            find(
                "request-identity-conflict",
                "The request identity already belongs to a lifecycle event.",
            )
            break

    return {
        "observed_state": observed_state,
        "outcome": "blocked" if findings else "ready",
        "findings": findings,
    }


def _inventory_record(
    inventory: dict[str, Any], kind: str, name: str
) -> dict[str, Any] | None:
    record = inventory.get(COLLECTIONS[kind], {}).get(name)
    if record is None and kind == "repo":
        record = inventory.get("retired_repos", {}).get(name)
    return record


def _target_history(history: dict[str, Any], record_id: str) -> list[dict[str, Any]]:
    return [
        event
        for event in history["events"]
        if event["target"]["record_id"] == record_id
    ]


def _validate_updated_value(
    kind: str,
    name: str,
    requested_value: dict[str, Any],
    record: dict[str, Any],
    contracts: InventoryContracts,
) -> None:
    collection = COLLECTIONS[kind]
    value = copy.deepcopy(requested_value)
    value["record"] = copy.deepcopy(record["record"])
    candidate: dict[str, Any] = {"schema_version": 2, collection: {name: value}}
    if kind == "repo":
        candidate["retired_repos"] = {}
    contracts.validate(f"{collection}.schema.json", candidate)
