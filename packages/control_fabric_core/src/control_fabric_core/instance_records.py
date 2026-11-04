"""Transactional storage for accepted installation-private operational records.

This is an internal persistence adapter, not a general client write API. The
required server-side validator owns readiness/decision validation. Source
contracts, Git caches and caller-provided `allowed` flags never authorize writes.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .canonical_json import canonicalization_errors
from .db.models import (
    InstanceOperationalOperation as Operation,
    InstanceOperationalRecord as Record,
    InstanceOperationalRevision as Revision,
)


KEY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}")
RECORD_ID = re.compile(r"repo:[a-z0-9][a-z0-9._-]{0,185}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MAX_COMMAND_BYTES = 131_072


class InstanceRecordError(RuntimeError):
    """A compact failure that never embeds project data or database errors."""


class InstanceRecordInvalid(InstanceRecordError):
    pass


class InstanceRecordConflict(InstanceRecordError):
    pass


class InstanceRecordUnavailable(InstanceRecordError):
    pass


def canonical_bytes(value: Any) -> bytes:
    if canonicalization_errors(value):
        raise InstanceRecordInvalid("record requires lossless canonical JSON")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def record_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _stored_digest(value: Any) -> str:
    try:
        return record_digest(value)
    except (InstanceRecordInvalid, RecursionError) as exc:
        raise InstanceRecordUnavailable("stored operational evidence is not canonical JSON") from exc


def _text(value: Any, maximum: int = 256) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _identity(instance_id: str, record_id: str | None = None) -> None:
    if not isinstance(instance_id, str) or not KEY.fullmatch(instance_id):
        raise InstanceRecordInvalid("invalid installation identity")
    if record_id is not None and (not isinstance(record_id, str) or not RECORD_ID.fullmatch(record_id)):
        raise InstanceRecordInvalid("invalid repository record identity")


def _command(value: dict[str, Any]) -> dict[str, Any]:
    required = {"schema_version", "instance_id", "record_id", "idempotency_key", "expected", "action", "record", "decision"}
    if not isinstance(value, dict) or set(value) != required or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise InstanceRecordInvalid("invalid internal record command")
    _identity(value["instance_id"], value["record_id"])
    if not _text(value["idempotency_key"]) or not _text(value["action"], 64):
        raise InstanceRecordInvalid("invalid operation identity")
    expected = value["expected"]
    if not isinstance(expected, dict) or set(expected) != {"version", "digest"}:
        raise InstanceRecordInvalid("exact expected record state is required")
    if not (expected == {"version": None, "digest": None} or (
        type(expected["version"]) is int and expected["version"] > 0
        and isinstance(expected["digest"], str) and DIGEST.fullmatch(expected["digest"])
    )):
        raise InstanceRecordInvalid("invalid expected record state")
    if not isinstance(value["record"], dict) or not isinstance(value["decision"], dict):
        raise InstanceRecordInvalid("record and accepted decision are required")
    if len(canonical_bytes(value)) > MAX_COMMAND_BYTES:
        raise InstanceRecordInvalid("internal record command exceeds its byte limit")
    return copy.deepcopy(value)


def _provider(value: dict[str, Any]) -> tuple[str | None, str | None]:
    if not isinstance(value, dict):
        raise InstanceRecordInvalid("invalid repository record value")
    binding = value.get("provider_repository")
    if binding is None:
        return None, None
    if (not isinstance(binding, dict) or set(binding) != {"provider", "provider_repository_id", "full_name"}
            or binding["provider"] != "github"
            or not isinstance(binding["provider_repository_id"], str)
            or not re.fullmatch(r"[1-9][0-9]{0,31}", binding["provider_repository_id"])
            or not isinstance(binding["full_name"], str)
            or len(binding["full_name"]) > 256
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", binding["full_name"])):
        raise InstanceRecordInvalid("invalid immutable provider repository binding")
    return binding["provider"], binding["provider_repository_id"]


def catalog_conditions(instance_id: str, query: str = "", scope: str = "all"):
    _identity(instance_id)
    if not isinstance(query, str) or len(query) > 128 or scope not in {"all", "inventory", "eligible"}:
        raise InstanceRecordInvalid("invalid internal repository catalog selector")
    search = query.strip().lower()
    conditions = [Record.instance_id == instance_id]
    if search:
        conditions.append(Record.record_id.contains(search, autoescape=True))
    if scope != "all":
        conditions.append(Record.value["phase"].as_string() == ("inventory" if scope == "inventory" else "intake"))
    if scope == "eligible":
        conditions.append(Record.value["status"].as_string() == "admitted")
    return conditions


class InstanceRecordStore:
    def __init__(
        self, *, session_factory: sessionmaker[Session],
        validate_transition: Callable[[dict, dict | None, str], dict],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(validate_transition):
            raise InstanceRecordUnavailable("a server-side transition validator is required")
        self.sessions = session_factory
        self.validate_transition = validate_transition
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _result(self, revision: Revision) -> dict:
        value, receipt = copy.deepcopy(revision.value), copy.deepcopy(revision.receipt)
        if not isinstance(value, dict) or not isinstance(receipt, dict):
            raise InstanceRecordUnavailable("stored operational revision integrity failed")
        claimed = receipt.pop("receipt_digest", None)
        if (not isinstance(receipt, dict) or _stored_digest(receipt) != claimed
                or receipt.get("record_digest") != _stored_digest(value)
                or receipt.get("record_id") != revision.record_id
                or receipt.get("instance_id") != revision.instance_id
                or receipt.get("version") != revision.version):
            raise InstanceRecordUnavailable("stored operational revision integrity failed")
        receipt["receipt_digest"] = claimed
        return {"record": value, "receipt": receipt}

    def _operation_result(self, operation: Operation | None, revision: Revision) -> dict:
        result = self._result(revision)
        receipt = result["receipt"]
        if (operation is None or not isinstance(operation.command, dict)
                or _stored_digest(operation.command) != operation.command_digest
                or operation.instance_id != revision.instance_id
                or operation.command.get("instance_id") != revision.instance_id
                or operation.command.get("record_id") != revision.record_id
                or operation.command.get("idempotency_key") != operation.idempotency_key
                or operation.idempotency_key != receipt.get("idempotency_key")
                or operation.actor != receipt.get("actor")
                or operation.ai_decision_id != receipt.get("ai_decision_id")
                or operation.command_digest != receipt.get("command_digest")
                or operation.result != result):
            raise InstanceRecordUnavailable("operational record decision evidence integrity failed")
        return result

    def _current_result(self, row: Record | None, revision: Revision | None, operation: Operation | None) -> dict | None:
        if row is None:
            return None
        if revision is None:
            raise InstanceRecordUnavailable("operational record revision is missing")
        result = self._operation_result(operation, revision)
        try:
            provider = _provider(row.value)
        except InstanceRecordInvalid as exc:
            raise InstanceRecordUnavailable("stored provider binding integrity failed") from exc
        if (_stored_digest(row.value) != row.record_digest
                or row.record_digest != result["receipt"]["record_digest"]
                or provider != (row.provider, row.provider_repository_id)):
            raise InstanceRecordUnavailable("stored operational record integrity failed")
        return result

    def _current(self, session: Session, row: Record | None) -> dict | None:
        if row is None:
            return None
        revision = session.get(Revision, (row.instance_id, row.record_id, row.version))
        key = revision.receipt.get("idempotency_key") if revision and isinstance(revision.receipt, dict) else None
        operation = session.get(Operation, (row.instance_id, key)) if key is not None else None
        return self._current_result(row, revision, operation)

    def _replay(self, session: Session, operation: Operation, command: dict, actor: str) -> dict:
        if not isinstance(operation.command, dict) or _stored_digest(operation.command) != operation.command_digest:
            raise InstanceRecordUnavailable("stored operation command integrity failed")
        if operation.actor != actor or operation.command_digest != record_digest(command):
            raise InstanceRecordConflict("operation identity is already bound to another accepted command")
        if not isinstance(operation.result, dict) or not isinstance(operation.result.get("receipt"), dict):
            raise InstanceRecordUnavailable("stored operation replay integrity failed")
        receipt = operation.result["receipt"]
        revision = session.get(Revision, (command["instance_id"], command["record_id"], receipt.get("version")))
        if revision is None:
            raise InstanceRecordUnavailable("stored operation replay integrity failed")
        return self._operation_result(operation, revision)

    def commit(self, command: dict, *, actor: str) -> dict:
        """Atomically persist a server-validated decision or read its exact replay."""
        command = _command(command)
        if not _text(actor):
            raise InstanceRecordInvalid("authenticated caller is required")
        instance, identity = command["instance_id"], command["record_id"]
        operation_key = (instance, command["idempotency_key"])
        try:
            with self.sessions.begin() as session:
                operation = session.get(Operation, operation_key)
                if operation is not None:
                    return self._replay(session, operation, command, actor)
                row = session.scalar(select(Record).where(
                    Record.instance_id == instance, Record.record_id == identity,
                ).with_for_update())
                current = self._current(session, row)
                expected = command["expected"]
                observed = ({"version": row.version, "digest": row.record_digest} if row else {"version": None, "digest": None})
                if expected != observed:
                    raise InstanceRecordConflict("operational record changed; review its current state")
                # Local, server-owned policy adapter checks current authority,
                # readiness, legacy overlap and authenticated operator acceptance.
                evidence = self.validate_transition(copy.deepcopy(command), copy.deepcopy(current), actor)
                fields = {"policy_ref", "decision_digest", "readiness_digest", "operator_ref"}
                if (not isinstance(evidence, dict) or set(evidence) - {"ai_decision_id"} != fields
                        or not _text(evidence["policy_ref"], 512) or not _text(evidence["operator_ref"])
                        or any(not isinstance(evidence[key], str) or not DIGEST.fullmatch(evidence[key])
                               for key in ("decision_digest", "readiness_digest"))):
                    raise InstanceRecordUnavailable("transition validation evidence is unavailable")
                suggestion = evidence.get("ai_decision_id")
                if suggestion is not None and not _text(suggestion):
                    raise InstanceRecordUnavailable("accepted AI decision identity is invalid")
                evidence = {key: value for key, value in evidence.items() if value is not None}
                provider, provider_id = _provider(command["record"])
                if row and (row.provider, row.provider_repository_id) != (provider, provider_id):
                    raise InstanceRecordConflict("provider repository identity cannot be substituted or removed")
                version = row.version + 1 if row else 1
                now = self.clock()
                if now.tzinfo is None:
                    raise InstanceRecordUnavailable("operational record clock requires a timezone")
                value = command["record"]
                digest = record_digest(value)
                receipt = {"schema_version": 1, "artifact_type": "instance-operational-record-receipt",
                           "authority": "internal-instance-record", "instance_id": instance,
                           "record_id": identity, "version": version, "record_digest": digest,
                           "previous": observed, "idempotency_key": command["idempotency_key"],
                           "command_digest": record_digest(command), "actor": actor,
                           "action": command["action"], "accepted_at": now.astimezone(timezone.utc).isoformat(),
                           **evidence}
                receipt["receipt_digest"] = record_digest(receipt)
                result = {"record": copy.deepcopy(value), "receipt": receipt}
                if row:
                    changed = session.execute(update(Record).where(
                        Record.instance_id == instance, Record.record_id == identity,
                        Record.version == expected["version"], Record.record_digest == expected["digest"],
                    ).values(version=version, record_digest=digest, value=value))
                    if changed.rowcount != 1:
                        raise InstanceRecordConflict("concurrent operational record change")
                else:
                    session.add(Record(instance_id=instance, record_id=identity, version=version,
                                       record_digest=digest, provider=provider,
                                       provider_repository_id=provider_id, value=value))
                session.add(Revision(instance_id=instance, record_id=identity, version=version, value=value, receipt=receipt))
                session.add(Operation(instance_id=instance, idempotency_key=command["idempotency_key"],
                                      actor=actor, ai_decision_id=suggestion,
                                      command_digest=record_digest(command), command=command, result=result))
                session.flush()
                # Read the persisted payload, not merely the input, in this same
                # transaction. A successful return occurs after transaction commit.
                session.expire_all()
                durable = session.get(Revision, (instance, identity, version))
                if durable is None or self._result(durable) != result:
                    raise InstanceRecordUnavailable("operational record readback failed")
                return copy.deepcopy(result)
        except IntegrityError:
            # Concurrent identical calls can both initially see no operation.
            # Only an exact durable replay resolves that race as success.
            try:
                with self.sessions() as session:
                    operation = session.get(Operation, operation_key)
                    if operation is not None:
                        return self._replay(session, operation, command, actor)
            except SQLAlchemyError as exc:
                raise InstanceRecordUnavailable("operational record storage is unavailable") from exc
            raise InstanceRecordConflict("record or provider identity was concurrently claimed") from None
        except SQLAlchemyError as exc:
            raise InstanceRecordUnavailable("operational record storage is unavailable") from exc

    def read(self, *, instance_id: str, record_id: str) -> dict | None:
        _identity(instance_id, record_id)
        try:
            with self.sessions() as session:
                return self._current(session, session.get(Record, (instance_id, record_id)))
        except SQLAlchemyError as exc:
            raise InstanceRecordUnavailable("operational record storage is unavailable") from exc

    def read_by_provider(self, *, instance_id: str, provider: str, provider_repository_id: str) -> dict | None:
        _identity(instance_id)
        if provider != "github" or not isinstance(provider_repository_id, str) or not re.fullmatch(r"[1-9][0-9]{0,31}", provider_repository_id):
            raise InstanceRecordInvalid("select one immutable provider repository identity")
        try:
            with self.sessions() as session:
                row = session.execute(select(Record).where(Record.instance_id == instance_id,
                    Record.provider == provider, Record.provider_repository_id == provider_repository_id)).scalar_one_or_none()
                return self._current(session, row)
        except SQLAlchemyError as exc:
            raise InstanceRecordUnavailable("operational record storage is unavailable") from exc

    def operation(self, *, instance_id: str, idempotency_key: str, actor: str | None = None) -> dict | None:
        """Internal recovery read; the API must compare original operator input.

        This preserves a server-produced plan after a lost acknowledgement. It
        never replans a committed operation against newer policy/state or grants
        a new source effect. No public endpoint exposes complete commands.
        """
        _identity(instance_id)
        if not _text(idempotency_key) or actor is not None and not _text(actor):
            raise InstanceRecordInvalid("authenticated operation identity is required")
        try:
            with self.sessions() as session:
                operation = session.get(Operation, (instance_id, idempotency_key))
                if operation is None:
                    return None
                if actor is not None and operation.actor != actor:
                    raise InstanceRecordConflict("operation belongs to another authenticated caller")
                result = self._replay(session, operation, operation.command, operation.actor)
                return {"command": copy.deepcopy(operation.command), "result": result}
        except SQLAlchemyError as exc:
            raise InstanceRecordUnavailable("operational record storage is unavailable") from exc

    def list(self, *, instance_id: str, after: str | None = None, limit: int = 25,
             query_text: str = "", scope: str = "all", record_ids: list[str] | None = None) -> dict:
        _identity(instance_id, after)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InstanceRecordInvalid("record page size must be from 1 through 100")
        conditions = catalog_conditions(instance_id, query_text, scope)
        if record_ids is not None:
            if (not isinstance(record_ids, list) or not 1 <= len(record_ids) <= 25 or any(not isinstance(value, str) for value in record_ids) or record_ids != sorted(set(record_ids))
                    or after is not None or query_text or scope != "all"):
                raise InstanceRecordInvalid("select at most 25 exact ordered repository identities")
            for record_id in record_ids: _identity(instance_id, record_id)
            conditions.append(Record.record_id.in_(record_ids))
        try:
            with self.sessions() as session:
                query = select(Record, Revision, Operation).outerjoin(Revision, (
                    (Record.instance_id == Revision.instance_id) & (Record.record_id == Revision.record_id)
                    & (Record.version == Revision.version)
                )).outerjoin(Operation, (
                    (Operation.instance_id == Revision.instance_id)
                    & (Operation.idempotency_key == Revision.receipt["idempotency_key"].as_string())
                )).where(*conditions)
                if after is not None:
                    query = query.where(Record.record_id > after)
                rows = list(session.execute(query.order_by(Record.record_id).limit(limit + 1)))
                return {"records": [self._current_result(*row) for row in rows[:limit]],
                        "next_after": rows[limit - 1][0].record_id if len(rows) > limit else None}
        except SQLAlchemyError as exc:
            raise InstanceRecordUnavailable("operational record storage is unavailable") from exc

    def history(self, *, instance_id: str, record_id: str, before_version: int | None = None, limit: int = 25) -> dict:
        _identity(instance_id, record_id)
        if (type(limit) is not int or not 1 <= limit <= 100
                or before_version is not None and (type(before_version) is not int or before_version < 1)):
            raise InstanceRecordInvalid("invalid record history page")
        try:
            with self.sessions() as session:
                current = self._current(session, session.get(Record, (instance_id, record_id)))
                if current is None:
                    return {"found": False, "records": [], "next_before_version": None}
                head = current["receipt"]["version"]
                start = min(head, before_version - 1) if before_version is not None else head
                query = select(Revision, Operation).outerjoin(Operation, (
                    (Operation.instance_id == Revision.instance_id)
                    & (Operation.idempotency_key == Revision.receipt["idempotency_key"].as_string())
                )).where(Revision.instance_id == instance_id, Revision.record_id == record_id,
                         Revision.version <= start).order_by(Revision.version.desc()).limit(limit + 1)
                rows = list(session.execute(query))
                if len(rows) != min(start, limit + 1) or any(row[0].version != start - index for index, row in enumerate(rows)):
                    raise InstanceRecordUnavailable("operational record history is incomplete")
                results = [self._operation_result(operation, revision) for revision, operation in rows]
                for newer, older in zip(results, results[1:]):
                    receipt = newer["receipt"]
                    if receipt["previous"] != {"version": older["receipt"]["version"], "digest": older["receipt"]["record_digest"]}:
                        raise InstanceRecordUnavailable("operational record history binding failed")
                return {"found": True, "records": results[:limit],
                        "next_before_version": rows[limit - 1][0].version if len(rows) > limit else None}
        except SQLAlchemyError as exc:
            raise InstanceRecordUnavailable("operational record storage is unavailable") from exc
