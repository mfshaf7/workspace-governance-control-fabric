"""Authenticated, read-only Workspace Registry and product continuity projection."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .artifact_registry import (
    DEFAULT_OOS_CALLER_ID,
    OOS_CALLER_ID_ENV,
    RUNTIME_PROFILE_ENV,
)
from .database import create_session_factory
from .db.models import ProductPublicationReadinessRecord
from .workspace_inventory_contracts import (
    COLLECTIONS,
    InventoryAuthority,
    InventoryContracts,
    InventorySnapshot,
    InventoryUnavailable,
    artifact_digest,
    digest,
)


CONTINUITY_PROJECTION_ENABLED_ENV = "WGCF_WORKSPACE_CONTINUITY_PROJECTION_ENABLED"
PROJECTION_SCHEMA_PATH_ENV = "WGCF_WORKSPACE_CONTINUITY_SCHEMA_PATH"
DEFAULT_PROJECTION_SCHEMA_PATH = (
    Path(__file__).resolve().parents[4]
    / "schemas"
    / "workspace-continuity-projection.schema.json"
)


def projection_schema_path() -> Path:
    configured = os.environ.get(PROJECTION_SCHEMA_PATH_ENV, "").strip()
    if not configured:
        return DEFAULT_PROJECTION_SCHEMA_PATH
    path = Path(configured)
    if not path.is_absolute():
        raise ValueError(f"{PROJECTION_SCHEMA_PATH_ENV} must be an absolute path")
    return path


class WorkspaceContinuityProjectionUnavailable(RuntimeError):
    """Canonical authority or stored readiness cannot produce a trusted projection."""


class WorkspaceContinuityProjectionService:
    """Join committed registry, intake, lifecycle, and readiness truth without mutation."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        authority: InventoryAuthority,
        clock: Callable[[], datetime] | None = None,
        readiness_actor: str | None = None,
    ) -> None:
        self.sessions = session_factory
        self.authority = authority
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.readiness_actor = (
            readiness_actor or os.environ.get(OOS_CALLER_ID_ENV, DEFAULT_OOS_CALLER_ID)
        ).strip()
        if not self.readiness_actor:
            raise WorkspaceContinuityProjectionUnavailable(
                "Product Publication workflow identity is unavailable",
            )
        try:
            schema = json.loads(projection_schema_path().read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            self.validator = Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            )
        except (OSError, ValueError, SchemaError) as exc:
            raise WorkspaceContinuityProjectionUnavailable(
                "workspace continuity projection contract is unavailable",
            ) from exc

    def read(self) -> dict[str, Any]:
        try:
            snapshot = self.authority.snapshot()
            inventory, by_id = _inventory(snapshot)
            intake = _intake(snapshot, set(by_id))
            lifecycle = _lifecycle(snapshot, by_id)
            readiness = self._publication_readiness(snapshot, by_id)
            products = _products(by_id, lifecycle, readiness)
        except WorkspaceContinuityProjectionUnavailable:
            raise
        except InventoryUnavailable as exc:
            raise WorkspaceContinuityProjectionUnavailable(
                "committed Workspace Governance authority is unavailable",
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceContinuityProjectionUnavailable(
                "workspace continuity authority is contradictory",
            ) from exc

        projection = {
            "schema_version": 1,
            "artifact_type": "workspace-continuity-projection",
            "projection_state": "current",
            "authority": {
                "owner_repo": "workspace-governance",
                "revision": snapshot.revision,
                "file_digests": copy.deepcopy(snapshot.file_digests),
            },
            "authority_boundary": {
                "canonical_registry": "workspace-governance",
                "readiness_projection": "workspace-governance-control-fabric",
                "workflow_authority": "operator-orchestration-service",
                "terminal_publication_authority": "operator-orchestration-service",
                "mutation_authority": "none",
            },
            "inventory": inventory,
            "intake": intake,
            "products": products,
        }
        projection["projection_digest"] = digest(projection)
        projection["projection_id"] = (
            "workspace-continuity-projection:"
            + projection["projection_digest"].removeprefix("sha256:")
        )
        errors = sorted(
            self.validator.iter_errors(projection),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            raise WorkspaceContinuityProjectionUnavailable(
                "workspace continuity projection violates its response contract",
            )
        return projection

    def _publication_readiness(
        self,
        snapshot: InventorySnapshot,
        by_id: dict[str, tuple[str, str, dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        now = self.clock().astimezone(timezone.utc)
        latest: dict[str, dict[str, Any]] = {}
        product_ids = sorted(
            record_id for record_id, (kind, _, _) in by_id.items() if kind == "product"
        )
        try:
            with self.sessions() as session:
                for product_id in product_ids:
                    row = session.scalar(
                        select(ProductPublicationReadinessRecord)
                        .where(
                            ProductPublicationReadinessRecord.actor
                            == self.readiness_actor,
                            ProductPublicationReadinessRecord.readiness[
                                "product_id"
                            ].as_string()
                            == product_id,
                        )
                        .order_by(
                            ProductPublicationReadinessRecord.created_at.desc(),
                            ProductPublicationReadinessRecord.evaluation_id.desc(),
                        )
                        .limit(1)
                    )
                    if row is not None:
                        latest[product_id] = _readiness_record(
                            row,
                            snapshot,
                            by_id,
                            now,
                        )
            return latest
        except SQLAlchemyError as exc:
            raise WorkspaceContinuityProjectionUnavailable(
                "Product Publication readiness ledger is unavailable",
            ) from exc


def _inventory(
    snapshot: InventorySnapshot,
) -> tuple[list[dict[str, Any]], dict[str, tuple[str, str, dict[str, Any]]]]:
    projected: list[dict[str, Any]] = []
    by_id: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for kind, collection in COLLECTIONS.items():
        source = snapshot.records[collection]
        groups = [(source[collection], False)]
        if kind == "repo":
            groups.append((source.get("retired_repos", {}), True))
        for records, retired_collection in groups:
            for name, value in records.items():
                record = value["record"]
                record_id = f"{kind}:{name}"
                if record["id"] != record_id or record_id in by_id:
                    raise WorkspaceContinuityProjectionUnavailable(
                        "canonical inventory identity is missing or ambiguous",
                    )
                _validate_lineage(record["lineage"], record_id)
                by_id[record_id] = (kind, name, value)
                projected.append({
                    "record_id": record_id,
                    "kind": kind,
                    "name": name,
                    "record_version": record["version"],
                    "record_digest": digest(value),
                    "posture": value["posture"],
                    "lifecycle": value["lifecycle"],
                    "retired_collection": retired_collection,
                    "lineage": copy.deepcopy(record["lineage"]),
                    "last_mutation": copy.deepcopy(record["last_mutation"]),
                })
    projected.sort(key=lambda item: item["record_id"])
    return projected, by_id


def _validate_lineage(lineage: dict[str, Any], record_id: str) -> None:
    source = lineage["source"]
    version = lineage["intake_entry_version"]
    if source == "workspace-intake":
        if version is None or lineage["source_ref"] != record_id:
            raise WorkspaceContinuityProjectionUnavailable(
                "active inventory has invalid Workspace Intake lineage",
            )
    elif source == "legacy-migration":
        if version is not None:
            raise WorkspaceContinuityProjectionUnavailable(
                "legacy inventory lineage cannot claim an intake version",
            )
    else:
        raise WorkspaceContinuityProjectionUnavailable(
            "active inventory lineage source is unsupported",
        )


def _intake(snapshot: InventorySnapshot, active_ids: set[str]) -> list[dict[str, Any]]:
    register = snapshot.records["intake-register"]
    projected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, collection in COLLECTIONS.items():
        for name, value in register[collection].items():
            record = value["record"]
            record_id = f"{kind}:{name}"
            if record["id"] != record_id or record_id in seen or record_id in active_ids:
                raise WorkspaceContinuityProjectionUnavailable(
                    "Workspace Intake identity is missing, duplicated, or already active",
                )
            seen.add(record_id)
            projected.append({
                "record_id": record_id,
                "kind": kind,
                "name": name,
                "record_version": record["version"],
                "record_digest": digest(value),
                "status": value["status"],
                "decision_source": value["decision_source"],
                "owner_route": value["owner_route"],
                "source": copy.deepcopy(record["source"]),
                "decision": copy.deepcopy(record["decision"]),
                "last_mutation": copy.deepcopy(record["last_mutation"]),
            })
    projected.sort(key=lambda item: item["record_id"])
    return projected


def _lifecycle(
    snapshot: InventorySnapshot,
    by_id: dict[str, tuple[str, str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for event in snapshot.records["workspace-inventory-history"]["events"]:
        record_id = event["target"]["record_id"]
        if record_id not in by_id:
            raise WorkspaceContinuityProjectionUnavailable(
                "Workspace Inventory history references an absent identity",
            )
        latest[record_id] = event
    for record_id, event in latest.items():
        value = by_id[record_id][2]
        expected = {
            "record_version": value["record"]["version"],
            "record_digest": digest(value),
            "posture": value["posture"],
        }
        if event["after"] != expected:
            raise WorkspaceContinuityProjectionUnavailable(
                "Workspace Inventory history does not match current registry state",
            )
    return latest


def _readiness_record(
    row: ProductPublicationReadinessRecord,
    snapshot: InventorySnapshot,
    by_id: dict[str, tuple[str, str, dict[str, Any]]],
    now: datetime,
) -> dict[str, Any]:
    readiness = copy.deepcopy(row.readiness)
    if (
        not isinstance(readiness, dict)
        or readiness.get("readiness_digest") != row.readiness_digest
        or artifact_digest(readiness, "readiness_digest") != row.readiness_digest
        or readiness.get("product_id") not in by_id
        or by_id[readiness["product_id"]][0] != "product"
        or readiness.get("authority_binding", {}).get("revision") != row.authority_revision
    ):
        raise WorkspaceContinuityProjectionUnavailable(
            "stored Product Publication readiness has invalid identity or integrity",
        )
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    stale_reasons: list[str] = []
    if now >= expires_at:
        stale_reasons.append("expired")
    if row.authority_revision != snapshot.revision:
        stale_reasons.append("authority-revision-changed")
    control_state = "stale" if stale_reasons else "current"
    outcome = str(readiness.get("outcome") or "blocked")
    return {
        "product_id": readiness["product_id"],
        "control_state": control_state,
        "actionable": control_state == "current" and outcome == "ready",
        "outcome": outcome,
        "action": readiness.get("action"),
        "current_publication_posture": readiness.get("current_publication_posture"),
        "target_publication": copy.deepcopy(readiness.get("target_publication")),
        "readiness_ref": {
            "uri": "wgcf://readiness/product-publication/"
            + row.readiness_digest.removeprefix("sha256:"),
            "digest": row.readiness_digest,
        },
        "authority_revision": row.authority_revision,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        "stale_reasons": stale_reasons,
    }


def _products(
    by_id: dict[str, tuple[str, str, dict[str, Any]]],
    lifecycle: dict[str, dict[str, Any]],
    readiness: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for record_id, (kind, name, value) in sorted(by_id.items()):
        if kind != "product":
            continue
        event = lifecycle.get(record_id)
        publication = readiness.get(record_id) or {
            "product_id": record_id,
            "control_state": "unavailable",
            "actionable": False,
            "outcome": None,
            "action": None,
            "current_publication_posture": None,
            "target_publication": None,
            "readiness_ref": None,
            "authority_revision": None,
            "expires_at": None,
            "stale_reasons": ["no-readiness-evidence"],
        }
        projected.append({
            "product_id": record_id,
            "name": name,
            "record_version": value["record"]["version"],
            "record_digest": digest(value),
            "posture": value["posture"],
            "maturity": value["maturity"],
            "lifecycle": value["lifecycle"],
            "owners": {
                "platform": value["platform_owner"],
                "security": value["security_owner"],
                "runtime": value["runtime_owner"],
                "source": copy.deepcopy(value["source_owners"]),
            },
            "stage_supported": value["stage_supported"],
            "governed_prod_promotion": value["governed_prod_promotion"],
            "highest_real_endpoint": value["highest_real_endpoint"],
            "lineage": copy.deepcopy(value["record"]["lineage"]),
            "latest_lifecycle_event": None if event is None else {
                "id": event["event_id"],
                "digest": event["event_digest"],
                "sequence": event["sequence"],
                "action": event["action"],
                "applied_at": event["applied_at"],
            },
            "publication_readiness": publication,
        })
    return projected


def build_workspace_continuity_projection_runtime() -> WorkspaceContinuityProjectionService:
    contracts = InventoryContracts.load()
    if (
        os.environ.get(RUNTIME_PROFILE_ENV) != "dev-integration"
        or os.environ.get(CONTINUITY_PROJECTION_ENABLED_ENV) != "true"
        or contracts.manifest["runtime_activation"] is not True
    ):
        raise WorkspaceContinuityProjectionUnavailable(
            "workspace continuity projection awaits approved runtime activation",
        )
    root = os.environ.get("WGCF_WORKSPACE_GOVERNANCE_REPO_ROOT", "").strip()
    if not root:
        raise WorkspaceContinuityProjectionUnavailable(
            "workspace continuity authority is not configured",
        )
    return WorkspaceContinuityProjectionService(
        session_factory=create_session_factory(),
        authority=InventoryAuthority(Path(root), contracts),
    )
