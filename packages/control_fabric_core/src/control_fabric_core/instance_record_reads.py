"""Bounded installation-private metadata reads for the authenticated OOS reader."""

from __future__ import annotations

import os

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from .db.models import InstanceOperationalRecord

from .database import create_session_factory
from .instance_records import (
    InstanceRecordInvalid, InstanceRecordStore, InstanceRecordUnavailable,
    _identity, canonical_bytes, catalog_conditions,
)


MAX_RESPONSE_BYTES = 524_288


class InstanceRecordNotFound(RuntimeError):
    pass


class InstanceRecordReadService:
    def __init__(self, *, store: InstanceRecordStore, instance_id: str):
        _identity(instance_id)
        self.store = store
        self.instance_id = instance_id

    def _record_id(self, name: str) -> str:
        identity = f"repo:{name}"
        _identity(self.instance_id, identity)
        return identity

    def _authority(self) -> dict:
        return {"kind": "internal-instance-record", "instance_id": self.instance_id}

    def read(self, name: str) -> dict:
        result = self.store.read(instance_id=self.instance_id, record_id=self._record_id(name))
        if result is None:
            return {"schema_version": 1, "authority": self._authority(), "found": False, "record": None, "receipt": None}
        return {"schema_version": 1, "authority": self._authority(), "found": True, **result}

    def list(self, *, after: str | None = None, limit: int = 25) -> dict:
        page = self.store.list(instance_id=self.instance_id, after=after, limit=limit)
        records = []
        for result in page["records"]:
            value, receipt = result["record"], result["receipt"]
            phase, status = value.get("phase"), value.get("posture", value.get("status"))
            if status not in ({"out-of-scope", "proposed", "admitted"} if phase == "intake" else {"active", "suspended", "retired"} if phase == "inventory" else set()):
                raise InstanceRecordUnavailable("stored repository projection is invalid")
            # No full descriptions, historical commands, source contents or
            # per-record history fetches in a register listing.
            records.append({"record_id": receipt["record_id"], "version": receipt["version"],
                            "record_digest": receipt["record_digest"],
                            "phase": phase, "status": status,
                            "provider_repository": value.get("provider_repository")})
        return {"schema_version": 1, "authority": self._authority(),
                "records": records, "next_after": page["next_after"]}

    def lookup(self, names: list[str]) -> dict:
        if not isinstance(names, list) or not 1 <= len(names) <= 25 or any(not isinstance(name, str) for name in names) or names != sorted(set(names)):
            raise InstanceRecordInvalid("select at most 25 exact ordered repository names")
        identities = [self._record_id(name) for name in names]
        page = self.store.list(instance_id=self.instance_id, limit=25, record_ids=identities)
        records = []
        for result in page["records"]:
            value, receipt = result["record"], result["receipt"]
            phase, status = value.get("phase"), value.get("posture", value.get("status"))
            if status not in ({"out-of-scope", "proposed", "admitted"} if phase == "intake" else {"active", "suspended", "retired"} if phase == "inventory" else set()):
                raise InstanceRecordUnavailable("stored repository status is invalid")
            records.append({"record_id": receipt["record_id"], "version": receipt["version"], "record_digest": receipt["record_digest"],
                "phase": phase, "status": status, "provider_repository": value.get("provider_repository")})
        return {"schema_version": 1, "authority": self._authority(), "record_ids": identities, "records": records}

    def catalog(self, *, after: str | None = None, limit: int = 25, query: str = "", scope: str = "all") -> dict:
        """A bounded metadata page, never a full installation or history scan."""
        conditions = catalog_conditions(self.instance_id, query, scope)
        page = self.store.list(instance_id=self.instance_id, after=after, limit=limit, query_text=query, scope=scope)
        record = InstanceOperationalRecord
        phase = record.value["phase"].as_string()
        status = func.coalesce(record.value["posture"].as_string(), record.value["status"].as_string())
        counts = {"intake": {"out-of-scope": 0, "proposed": 0, "admitted": 0},
                  "inventory": {"active": 0, "suspended": 0, "retired": 0}}
        try:
            with self.store.sessions() as session:
                for kind, posture, count in session.execute(select(phase, status, func.count()).where(
                        *conditions).group_by(phase, status)):
                    if kind not in counts or posture not in counts[kind]:
                        raise InstanceRecordUnavailable("internal repository catalog contains invalid metadata")
                    counts[kind][posture] = count
        except SQLAlchemyError as exc:
            raise InstanceRecordUnavailable("internal repository catalog is unavailable") from exc
        result = {"schema_version": 1, "authority": self._authority(), "records": list(page["records"]),
                  "counts": counts, "selection": {"query": query.strip().lower(), "scope": scope}, "next_after": page["next_after"]}
        while result["records"] and len(canonical_bytes(result)) > MAX_RESPONSE_BYTES:
            result["records"].pop()
            result["next_after"] = result["records"][-1]["receipt"]["record_id"] if result["records"] else None
        if page["records"] and not result["records"]:
            raise InstanceRecordUnavailable("repository metadata cannot fit its bounded catalog page")
        return result

    def history(self, name: str, *, before_version: int | None = None, limit: int = 25) -> dict:
        identity = self._record_id(name)
        page = self.store.history(instance_id=self.instance_id, record_id=identity,
                                  before_version=before_version, limit=limit)
        if not page.pop("found"):
            raise InstanceRecordNotFound("internal repository record was not found")
        result = {"schema_version": 1, "authority": self._authority(), **page}
        while result["records"] and len(canonical_bytes(result)) > MAX_RESPONSE_BYTES:
            result["records"].pop()
            result["next_before_version"] = result["records"][-1]["receipt"]["version"] if result["records"] else None
        if not result["records"] and before_version != 1:
            raise InstanceRecordUnavailable("history result cannot fit its bounded response")
        return result


def build_instance_record_read_runtime(database_url: str | None = None) -> InstanceRecordReadService:
    if (os.environ.get("WGCF_INSTANCE_RECORDS_ENABLED") != "true"
            or os.environ.get("WGCF_RUNTIME_PROFILE") != "dev-integration"):
        raise InstanceRecordUnavailable("internal operational records are not enabled in this composition")
    instance_id = os.environ.get("WGCF_INSTANCE_ID", "").strip()
    try:
        _identity(instance_id)
    except InstanceRecordInvalid as exc:
        raise InstanceRecordUnavailable("the installation identity is not configured") from exc

    def deny_write(command, current, actor):
        raise InstanceRecordUnavailable("the read-only adapter cannot accept a record transition")

    return InstanceRecordReadService(instance_id=instance_id, store=InstanceRecordStore(
        session_factory=create_session_factory(database_url), validate_transition=deny_write,
    ))
