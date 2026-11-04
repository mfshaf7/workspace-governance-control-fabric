"""Accepted internal repository decisions using the native Governance policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .instance_records import (
    InstanceRecordConflict, InstanceRecordInvalid, InstanceRecordStore,
    InstanceRecordUnavailable, _identity, canonical_bytes, record_digest,
)
from .database import create_session_factory
from .workspace_intake_contracts import IntakeAuthority, IntakeContracts, IntakeUnavailable


@dataclass(frozen=True)
class InstancePolicyContext:
    revision: str
    policy_ref: dict
    legacy_records: dict
    helper: Path


class NativeInstancePolicy:
    """Execute only the current clean, verified native policy source."""

    def __init__(self, authority: IntakeAuthority):
        self.authority = authority

    def context(self) -> InstancePolicyContext:
        try:
            snapshot = self.authority.snapshot()
            root = self.authority.repo_root.resolve()
            if (self.authority._git("rev-parse", "HEAD").decode().strip() != snapshot.revision
                    or self.authority._git("-c", "core.fsmonitor=false", "status", "--porcelain", "--untracked-files=normal").strip()
                    or (root / "scripts").is_symlink()):
                raise InstanceRecordUnavailable("native instance policy requires its verified clean source")
            for name in ("scripts/workspace_instance_records.py", "contracts/instance-operational-records.yaml",
                         "contracts/schemas/instance-repository-intake-request.schema.json"):
                source = root / name
                if source.is_symlink() or not source.is_file() or source.read_bytes() != self.authority._git("show", f"{snapshot.revision}:{name}"):
                    raise InstanceRecordUnavailable("native instance policy source is unavailable")
            contract = (root / "contracts/instance-operational-records.yaml").read_bytes()
            legacy = {}
            for collection in (snapshot.records["intake-register"].get("repos", {}),
                               snapshot.records["repos"].get("repos", {}),
                               snapshot.records["repos"].get("retired_repos", {})):
                if set(legacy) & set(collection):
                    raise InstanceRecordUnavailable("legacy repository authority overlaps")
                if any(not isinstance(value, dict) for value in collection.values()):
                    raise InstanceRecordUnavailable("legacy repository authority is invalid")
                legacy.update(copy.deepcopy(collection))
            return InstancePolicyContext(snapshot.revision,
                {"repo": "workspace-governance", "revision": snapshot.revision,
                 "contract_digest": "sha256:" + hashlib.sha256(contract).hexdigest()},
                legacy, root / "scripts/workspace_instance_records.py")
        except (OSError, ValueError, IntakeUnavailable) as exc:
            raise InstanceRecordUnavailable("native instance policy authority is unavailable") from exc

    def plan(self, context: InstancePolicyContext, *, request: dict, decision: dict,
             instance_id: str, operator_ref: str, current: dict | None, now: datetime, owner_readiness: dict | None) -> dict:
        target = request.get("target", {}).get("name")
        provider = request.get("requested_record", {}).get("provider_repository")
        ai = decision.get("ai_suggestion")
        suggestion = ai.get("decision_id") if isinstance(ai, dict) else None
        # Only matching legacy claims cross the finite policy bridge. This is
        # not a full-register payload or a per-project history fan-out.
        relevant = {name: value for name, value in context.legacy_records.items() if (
            name == target or provider is not None and isinstance(value.get("provider_repository"), dict)
            and all(value["provider_repository"].get(field) == provider.get(field) for field in ("provider", "provider_repository_id"))
            or suggestion is not None and value.get("ai_suggestion", {}).get("decision_id") == suggestion
        )}
        payload = {"policy_revision": context.revision, "instance_id": instance_id, "request": request, "decision": decision,
                   "operator_ref": operator_ref, "current": current, "legacy_records": relevant,
                   "now": now.isoformat(), "owner_readiness": owner_readiness}
        output = self._run(context, payload)
        try:
            result = json.loads(output)
            if (not isinstance(result, dict) or set(result) != {"record", "readiness", "readiness_digest", "decision_digest", "ai_decision_id"}
                    or not isinstance(result["readiness"], dict)
                    or result["readiness_digest"] != record_digest(result["readiness"])
                    or result["decision_digest"] != decision.get("decision_digest")
                    or result["readiness"].get("policy_ref") != context.policy_ref
                    or result["readiness"].get("outcome") != "allowed"):
                raise ValueError()
            return result
        except (ValueError, TypeError, KeyError) as exc:
            raise InstanceRecordUnavailable("native instance policy result integrity failed") from exc


    @staticmethod
    def _run(context: InstancePolicyContext, payload: dict) -> bytes:
        raw = canonical_bytes(payload)
        if len(raw) > 262_144:
            raise InstanceRecordInvalid("instance policy input exceeds its read bound")
        try:
            process = subprocess.run([sys.executable, "-B", "-s", str(context.helper)],
                input=raw, capture_output=True, timeout=30, cwd=context.helper.parent.parent,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InstanceRecordUnavailable("native instance policy could not be evaluated") from exc
        if process.returncode == 2:
            raise InstanceRecordConflict("the current policy did not allow this accepted registration; review its current state and evidence")
        if process.returncode != 0 or len(process.stdout) > 262_144:
            raise InstanceRecordUnavailable("native instance policy did not return a bounded result")
        return process.stdout


    def source_authority(self, context: InstancePolicyContext, *, instance_id: str,
                         name: str, current: dict, operation: str) -> dict:
        raw = self._run(context, {"operation": "source-authority", "instance_id": instance_id,
            "name": name, "current": current, "source_operation": operation, "policy_revision": context.revision})
        try:
            result = json.loads(raw)
            expected = {"kind": "internal-instance-record", "instance_id": instance_id, "record_id": f"repo:{name}"}
            if (not isinstance(result, dict) or result.get("canonical_authority") != expected
                    or result.get("policy_ref") != context.policy_ref
                    or result.get("record_ref") != {"version": current["receipt"]["version"], "digest": current["receipt"]["record_digest"]}
                    or any(result.get(field) is not False for field in ("model_writes_allowed", "canonical_mutation", "runtime_activation"))):
                raise ValueError()
            return result
        except (ValueError, TypeError, KeyError) as exc:
            raise InstanceRecordUnavailable("native source authority result integrity failed") from exc


class InstanceRepositoryIntakeService:
    def __init__(self, *, session_factory: sessionmaker[Session], instance_id: str,
                 policy: NativeInstancePolicy, clock: Callable[[], datetime] | None = None):
        _identity(instance_id)
        self.sessions, self.instance_id, self.policy = session_factory, instance_id, policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _store(self, validator=None, *, sessions=None) -> InstanceRecordStore:
        def deny(*args):
            raise InstanceRecordUnavailable("this internal adapter is read-only")
        return InstanceRecordStore(session_factory=sessions or self.sessions, validate_transition=validator or deny, clock=self.clock)

    def prepare(self, name: str) -> dict:
        identity = f"repo:{name}"
        _identity(self.instance_id, identity)
        context = self.policy.context()
        current = self._store().read(instance_id=self.instance_id, record_id=identity)
        if name in context.legacy_records:
            if current is not None:
                raise InstanceRecordConflict("internal and legacy authority overlap; explicit reconciliation is required")
            return {"schema_version": 1, "canonical_authority": {"kind": "legacy-git",
                    "repo": "workspace-governance", "revision": context.revision}, "record_id": identity}
        return {"schema_version": 1, "canonical_authority": {"kind": "internal-instance-record",
                "instance_id": self.instance_id, "record_id": identity}, "policy_ref": context.policy_ref,
                "expected_state": {"record_version": current["receipt"]["version"] if current else None,
                                   "record_digest": current["receipt"]["record_digest"] if current else None},
                "current": current}

    def source_authority(self, name: str, operation: str) -> dict:
        if operation not in {"bootstrap", "configure-verification", "ordinary-source"}:
            raise InstanceRecordInvalid("unsupported source authority operation")
        state = self.prepare(name)
        if state["canonical_authority"]["kind"] == "legacy-git":
            return state
        current = state["current"]
        if current is None:
            raise InstanceRecordConflict("the repository has no accepted internal registration")
        context = self.policy.context()
        if context.policy_ref != state["policy_ref"]:
            raise InstanceRecordConflict("source policy changed during the read")
        provider = current["record"].get("provider_repository", {})
        if any(isinstance(entry.get("provider_repository"), dict) and
               all(entry["provider_repository"].get(key) == provider.get(key) for key in ("provider", "provider_repository_id"))
               for entry in context.legacy_records.values()):
            raise InstanceRecordConflict("the source identity overlaps legacy authority")
        result = self.policy.source_authority(context, instance_id=self.instance_id, name=name, current=current, operation=operation)
        after = self.prepare(name)
        if after != state:
            raise InstanceRecordConflict("source owner changed during the read")
        return {"schema_version": 1, "canonical_authority": state["canonical_authority"], "source_authority": result}

    def source_authority_for_provider(self, provider_repository_id: str, operation: str) -> dict:
        """Resolve one physical repository without assuming its canonical alias.

        The unique provider key is scoped to this configured installation. This
        is not a catalog read and returns only the normal eligibility proof.
        """
        if (not isinstance(provider_repository_id, str) or not re.fullmatch(r"[1-9][0-9]{0,31}", provider_repository_id)
                or operation not in {"bootstrap", "configure-verification", "ordinary-source"}):
            raise InstanceRecordInvalid("select one exact provider identity and source operation")
        context = self.policy.context()
        current = self._store().read_by_provider(instance_id=self.instance_id, provider="github", provider_repository_id=provider_repository_id)
        legacy = [name for name, entry in context.legacy_records.items() if isinstance(entry.get("provider_repository"), dict)
                  and entry["provider_repository"].get("provider") == "github"
                  and entry["provider_repository"].get("provider_repository_id") == provider_repository_id]
        if len(legacy) > 1 or legacy and current is not None:
            raise InstanceRecordConflict("provider repository identity overlaps registration authorities")
        if current is None and not legacy:
            raise InstanceRecordConflict("the provider repository has no current canonical source registration")
        identity = current["receipt"]["record_id"] if current is not None else f"repo:{legacy[0]}"
        _identity(self.instance_id, identity)
        result = self.source_authority(identity.removeprefix("repo:"), operation)
        if self.policy.context().policy_ref != context.policy_ref:
            raise InstanceRecordConflict("source policy changed during provider identity resolution")
        if result["canonical_authority"]["kind"] == "internal-instance-record" and result["source_authority"]["provider_repository"]["provider_repository_id"] != provider_repository_id:
            raise InstanceRecordConflict("resolved source identity changed during the read")
        return result

    def accept(self, name: str, payload: dict, *, actor: str) -> dict:
        return self._within_operation(name, payload, lambda sessions: self._accept_locked(name, payload, actor=actor, sessions=sessions))

    def _within_operation(self, name: str, payload: dict, perform) -> dict:
        _identity(self.instance_id, f"repo:{name}")
        request = payload.get("request") if isinstance(payload, dict) else None
        key = request.get("idempotency_key") if isinstance(request, dict) else None
        if not isinstance(key, str) or not key.strip() or len(key) > 256:
            raise InstanceRecordInvalid("the original registration identity is required")
        # Serialize the whole decision, including planning and negative results.
        # Otherwise a timed-out first call could still commit after a retry had
        # incorrectly declared its original input rejected under newer policy.
        try:
            with self.sessions.begin() as scope:
                connection = scope.connection()
                if connection.dialect.name == "postgresql":
                    lock_id = int.from_bytes(hashlib.sha256(
                        ("wgcf-instance-decision-v1\0" + self.instance_id + "\0" + key).encode()
                    ).digest()[:8], "big", signed=True)
                    if not scope.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_id}):
                        raise InstanceRecordUnavailable("the original registration is still running; retry the same request")
                elif connection.dialect.name == "sqlite":
                    # The disposable SQLite proof uses its native transaction
                    # lock. Explicit BEGIN also prevents SAVEPOINT release from
                    # committing outside the enclosing request transaction.
                    scope.execute(text("BEGIN IMMEDIATE"))
                else:
                    raise InstanceRecordUnavailable("the database is not qualified for internal registration")
                sessions = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False,
                                        join_transaction_mode="create_savepoint")
                return perform(sessions)
        except SQLAlchemyError as exc:
            raise InstanceRecordUnavailable("internal registration storage is unavailable; retain the original request") from exc

    def resolve(self, name: str, payload: dict) -> dict:
        """Prove original outcome without evaluating policy or creating a record."""
        if (not isinstance(payload, dict) or set(payload) != {"schema_version", "request", "decision", "operator_ref"}
                or type(payload["schema_version"]) is not int or payload["schema_version"] != 1):
            raise InstanceRecordInvalid("the exact original registration is required for readback")
        request = payload["request"]
        if (not isinstance(request, dict) or request.get("instance_id") != self.instance_id
                or not isinstance(request.get("target"), dict) or request["target"].get("record_id") != f"repo:{name}"):
            raise InstanceRecordInvalid("original readback does not match this installation and repository")
        original = {key: payload[key] for key in ("request", "decision", "operator_ref")}

        def read(sessions):
            retained = self._store(sessions=sessions).operation(instance_id=self.instance_id, idempotency_key=request.get("idempotency_key"))
            result = retained["result"] if retained else {"record": None, "receipt": None}
            if retained and record_digest(retained["command"]["decision"].get("operator_input")) != record_digest(original):
                raise InstanceRecordConflict("original readback input differs from the accepted operation")
            return {"schema_version": 1, "authority": {"kind": "internal-instance-record", "instance_id": self.instance_id},
                    "found": retained is not None, **result}

        return self._within_operation(name, payload, read)

    def _accept_locked(self, name: str, payload: dict, *, actor: str, sessions: sessionmaker[Session]) -> dict:
        required = {"schema_version", "request", "decision", "operator_ref", "source_attestation", "owner_readiness"}
        if (not isinstance(payload, dict) or set(payload) != required
                or type(payload["schema_version"]) is not int or payload["schema_version"] != 1):
            raise InstanceRecordInvalid("invalid internal registration envelope")
        request, decision, operator = payload["request"], payload["decision"], payload["operator_ref"]
        if (not isinstance(request, dict) or not isinstance(decision, dict) or not isinstance(operator, str)
                or not operator.strip() or len(operator) > 256):
            raise InstanceRecordInvalid("accepted registration input is invalid")
        target = request.get("target")
        if (not isinstance(target, dict) or not isinstance(request.get("requested_record"), dict)
                or target.get("name") != name or target.get("record_id") != f"repo:{name}"):
            raise InstanceRecordInvalid("registration route does not match its exact repository")
        if request.get("instance_id") != self.instance_id:
            raise InstanceRecordInvalid("the accepted registration belongs to another installation")
        original = {"request": request, "decision": decision, "operator_ref": operator}
        retained = self._store(sessions=sessions).operation(instance_id=self.instance_id, idempotency_key=request.get("idempotency_key"), actor=actor)
        if retained is not None:
            if record_digest(retained["command"]["decision"].get("operator_input")) != record_digest(original):
                raise InstanceRecordConflict("the registration identity already belongs to different accepted input")
            return retained["result"]
        attestation = payload["source_attestation"]
        if (not isinstance(attestation, dict) or set(attestation) != {"attested_by", "source", "provider_repository", "evidence_refs", "attestation_digest"}
                or attestation["attested_by"] != "operator-orchestration-service"
                or attestation["source"] != request.get("source")
                or attestation["provider_repository"] != request.get("requested_record", {}).get("provider_repository")
                or not isinstance(attestation["evidence_refs"], list) or not 1 <= len(attestation["evidence_refs"]) <= 8
                or any(not isinstance(value, str) or not value.strip() or len(value) > 1024 for value in attestation["evidence_refs"])
                or attestation["attestation_digest"] != record_digest({key: value for key, value in attestation.items() if key != "attestation_digest"})):
            raise InstanceRecordInvalid("canonical custody attestation does not bind this registration")
        now = self.clock()
        current = self._store(sessions=sessions).read(instance_id=self.instance_id, record_id=f"repo:{name}")
        context = self.policy.context()
        plan = self.policy.plan(context, request=request, decision=decision, instance_id=self.instance_id, operator_ref=operator,
                                current=current, now=now, owner_readiness=payload["owner_readiness"])
        command = {"schema_version": 1, "instance_id": self.instance_id, "record_id": f"repo:{name}",
                   "idempotency_key": request["idempotency_key"],
                   "expected": {"version": request["expected_state"]["record_version"], "digest": request["expected_state"]["record_digest"]},
                   "action": "intake." + request["action"], "record": plan["record"],
                   "decision": {"operator_input": original, "custody_attestation": attestation,
                                "readiness": plan["readiness"]}}

        def validate(candidate: dict, actual: dict | None, caller: str) -> dict:
            if caller != actor or candidate != command:
                raise InstanceRecordConflict("registration command binding changed")
            latest = self.policy.context()
            checked = self.policy.plan(latest, request=request, decision=decision, instance_id=self.instance_id, operator_ref=operator,
                                       current=actual, now=now, owner_readiness=payload["owner_readiness"])
            if checked != plan:
                raise InstanceRecordConflict("registration policy or readiness changed before commit")
            return {"policy_ref": f"git://workspace-governance/{latest.revision}/contracts/instance-operational-records.yaml@{latest.policy_ref['contract_digest']}",
                    "decision_digest": checked["decision_digest"], "readiness_digest": checked["readiness_digest"],
                    "operator_ref": operator, "ai_decision_id": checked["ai_decision_id"]}

        return self._store(validate, sessions=sessions).commit(command, actor=actor)


def build_instance_repository_intake_runtime() -> InstanceRepositoryIntakeService:
    if (os.environ.get("WGCF_INSTANCE_RECORDS_ENABLED") != "true"
            or os.environ.get("WGCF_RUNTIME_PROFILE") != "dev-integration"):
        raise InstanceRecordUnavailable("internal registration is not enabled in this composition")
    instance_id = os.environ.get("WGCF_INSTANCE_ID", "").strip()
    root = os.environ.get("WGCF_WORKSPACE_GOVERNANCE_REPO_ROOT", "").strip()
    if not root or not Path(root).is_absolute():
        raise InstanceRecordUnavailable("the current native Governance authority is not configured")
    try:
        _identity(instance_id)
        authority = IntakeAuthority(Path(root), IntakeContracts.load())
    except (InstanceRecordInvalid, IntakeUnavailable) as exc:
        raise InstanceRecordUnavailable("internal registration policy is not configured") from exc
    return InstanceRepositoryIntakeService(session_factory=create_session_factory(), instance_id=instance_id,
                                            policy=NativeInstancePolicy(authority))
