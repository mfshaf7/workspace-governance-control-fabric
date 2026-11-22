"""Reviewed transition from a retained Git registration to private inventory."""

from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import re

from .instance_records import (
    DIGEST,
    InstanceRecordConflict,
    InstanceRecordInvalid,
    InstanceRecordUnavailable,
    _identity,
    _text,
    record_digest,
)
from .workspace_intake_contracts import IntakeUnavailable, yaml_record


_COMMIT = re.compile(r"[0-9a-f]{40}")
_INVENTORY_PATH = "contracts/repos.yaml"
_INTAKE_PATH = "contracts/intake-register.yaml"


class InstanceRepositoryMigrationService:
    def __init__(self, repository_service):
        self.repositories = repository_service
        self.policy = repository_service.policy
        self.instance_id = repository_service.instance_id

    def _read_plan(self, context, name: str, revision: str) -> dict:
        # Revision was selected by the verified current authority or its exact
        # first parent. Request fields cannot choose a repository or Git path.
        try:
            inventory_bytes = self.policy.authority._git("show", f"{revision}:{_INVENTORY_PATH}")
            intake_bytes = self.policy.authority._git("show", f"{revision}:{_INTAKE_PATH}")
        except IntakeUnavailable as exc:
            raise InstanceRecordUnavailable("original Git registration is unavailable") from exc
        if max(len(inventory_bytes), len(intake_bytes)) > 4 * 1024 * 1024:
            raise InstanceRecordUnavailable("original Git registry exceeds its read limit")
        try:
            inventory, intake = yaml_record(inventory_bytes), yaml_record(intake_bytes)
        except IntakeUnavailable as exc:
            raise InstanceRecordUnavailable("original Git registry is invalid") from exc
        collections = {"intake": intake.get("repos"), "inventory": inventory.get("repos"),
                       "retired": inventory.get("retired_repos", {})}
        if not all(isinstance(value, dict) for value in collections.values()):
            raise InstanceRecordUnavailable("original Git authority collections are invalid")
        entry = collections["inventory"].get(name)
        binding = entry.get("provider_repository") if isinstance(entry, dict) else None
        if not isinstance(binding, dict):
            raise InstanceRecordConflict("select a current Git-backed source repository")
        identity_fields = ("provider", "provider_repository_id")
        relevant = {collection: {
            key: row for key, row in items.items()
            if key == name or isinstance(row, dict) and isinstance(row.get("provider_repository"), dict)
            and all(row["provider_repository"].get(field) == binding.get(field) for field in identity_fields)
        } for collection, items in collections.items()}
        file_digest = "sha256:" + hashlib.sha256(inventory_bytes).hexdigest()
        payload = {"operation": "plan-legacy-repository-migration", "source_revision": revision,
                   "name": name, "authority_records": relevant, "source_file_digest": file_digest,
                   "expected_provider_repository": binding}
        try:
            result = json.loads(self.policy._run(context, payload))
            digest = result.get("plan_digest")
            source = result["source"]
            if (result.get("artifact_type") != "legacy-repository-migration-plan"
                    or result.get("canonical_mutation") is not False
                    or digest != record_digest({k: v for k, v in result.items() if k != "plan_digest"})
                    or source != {"repository": "workspace-governance", "revision": revision, "path": _INVENTORY_PATH,
                                  "file_digest": file_digest, "record_id": f"repo:{name}", "record_digest": record_digest(entry),
                                  "original_envelope": entry["record"]}
                    or result["provider_repository"] != binding or result["proposed_record"] != {"phase": "inventory", **entry}):
                raise ValueError()
            return result
        except (ValueError, KeyError, AttributeError, TypeError) as exc:
            raise InstanceRecordUnavailable("migration plan did not bind the original Git evidence") from exc

    def prepare(self, name: str) -> dict:
        record_id = f"repo:{name}"
        _identity(self.instance_id, record_id)
        context = self.policy.context()
        plan = self._read_plan(context, name, context.revision)
        store = self.repositories._store()
        if (store.read(instance_id=self.instance_id, record_id=record_id) is not None
                or store.read_by_provider(instance_id=self.instance_id, provider="github",
                   provider_repository_id=plan["provider_repository"]["provider_repository_id"]) is not None):
            raise InstanceRecordConflict("an internal registration already owns the selected repository")
        if self.policy.context().policy_ref != context.policy_ref:
            raise InstanceRecordConflict("Git authority changed during migration preparation")
        return {**plan, "instance_id": self.instance_id}

    def accept(self, name: str, request: dict, *, actor: str) -> dict:
        record_id = f"repo:{name}"
        _identity(self.instance_id, record_id)
        required = {"schema_version", "instance_id", "record_id", "idempotency_key", "plan_digest",
                    "source_revision", "cutover_revision", "operator_acceptance"}
        if (not isinstance(request, dict) or set(request) != required or type(request["schema_version"]) is not int
                or request["schema_version"] != 1 or request["instance_id"] != self.instance_id
                or request["record_id"] != record_id or not _text(request["idempotency_key"], 128)
                or not DIGEST.fullmatch(str(request["plan_digest"]))
                or any(not _COMMIT.fullmatch(str(request[key])) for key in ("source_revision", "cutover_revision"))):
            raise InstanceRecordInvalid("migration requires the reviewed installation, record and exact Git revisions")
        acceptance = request["operator_acceptance"]
        if (not isinstance(acceptance, dict) or set(acceptance) != {"state", "operator_ref", "recorded_at"}
                or acceptance["state"] != "accepted" or not _text(acceptance["operator_ref"])
                or not acceptance["operator_ref"].startswith("operator:")):
            raise InstanceRecordInvalid("migration requires authenticated operator acceptance")
        try:
            accepted_at = datetime.fromisoformat(acceptance["recorded_at"])
            if accepted_at.tzinfo is None or accepted_at > self.repositories.clock():
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise InstanceRecordInvalid("migration acceptance time is invalid") from exc
        store = self.repositories._store()
        key = request["idempotency_key"]
        existing = store.operation(instance_id=self.instance_id, idempotency_key=key, actor=actor)
        if existing is not None:
            if existing["command"]["action"] != "inventory.migrate-legacy" or existing["command"]["decision"].get("request") != request:
                raise InstanceRecordConflict("migration replay differs from the original accepted request")
            return existing["result"]

        context = self.policy.context()
        if context.revision != request["cutover_revision"] or context.revision == request["source_revision"]:
            raise InstanceRecordConflict("the reviewed Git cutover revision must be current")
        try:
            parent = self.policy.authority._git("rev-parse", "--verify", f"{context.revision}^1").decode().strip()
        except IntakeUnavailable as exc:
            raise InstanceRecordUnavailable("the original Git cutover parent is unavailable") from exc
        if parent != request["source_revision"]:
            raise InstanceRecordConflict("the cutover must directly follow the reviewed original Git record")
        plan = self._read_plan(context, name, parent)
        if plan["plan_digest"] != request["plan_digest"]:
            raise InstanceRecordConflict("the original Git registration differs from the reviewed migration plan")
        binding = plan["provider_repository"]

        def validate(command, current, authenticated_actor):
            latest = self.policy.context()
            if (latest.policy_ref != context.policy_ref or current is not None or name in latest.legacy_records
                    or authenticated_actor != actor or any(isinstance(entry.get("provider_repository"), dict)
                    and all(entry["provider_repository"].get(field) == binding[field]
                            for field in ("provider", "provider_repository_id")) for entry in latest.legacy_records.values())):
                raise InstanceRecordConflict("the selected repository still overlaps a canonical registration")
            return {"policy_ref": f"git://workspace-governance/{context.revision}/contracts/instance-operational-records.yaml@{context.policy_ref['contract_digest']}",
                    "decision_digest": record_digest(command["decision"]), "readiness_digest": plan["plan_digest"],
                    "operator_ref": acceptance["operator_ref"]}

        record = copy.deepcopy(plan["proposed_record"])
        record["record"]["version"] = 1
        return self.repositories._store(validate).commit({
            "schema_version": 1, "instance_id": self.instance_id, "record_id": record_id, "idempotency_key": key,
            "expected": {"version": None, "digest": None}, "action": "inventory.migrate-legacy", "record": record,
            "decision": {"request": copy.deepcopy(request), "original": plan["source"]},
        }, actor=actor)
