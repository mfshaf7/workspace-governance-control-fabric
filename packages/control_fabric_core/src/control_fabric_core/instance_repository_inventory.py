"""Native private Inventory transitions in the existing record transaction."""
from __future__ import annotations

from dataclasses import replace
import json

from .instance_records import InstanceRecordConflict, InstanceRecordInvalid, InstanceRecordUnavailable, record_digest
from .instance_repository_intake import NativeInstancePolicy, InstanceRepositoryIntakeService


class NativeInstanceInventoryPolicy(NativeInstancePolicy):
    def context(self):
        context = super().context()
        root = self.authority.repo_root.resolve()
        for name in ("scripts/workspace_instance_inventory.py", "contracts/schemas/instance-repository-inventory-request.schema.json"):
            source = root / name
            if source.is_symlink() or not source.is_file() or source.read_bytes() != self.authority._git("show", f"{context.revision}:{name}"):
                raise InstanceRecordUnavailable("native inventory policy source is unavailable")
        return replace(context, helper=root / "scripts/workspace_instance_inventory.py")

    def plan_inventory(self, context, *, request, instance_id, operator_ref, current, now):
        target = request.get("target", {}).get("name")
        provider = (current or {}).get("record", {}).get("provider_repository")
        relevant = {name: value for name, value in context.legacy_records.items() if name == target or (
            isinstance(provider, dict) and isinstance(value.get("provider_repository"), dict)
            and all(value["provider_repository"].get(key) == provider.get(key) for key in ("provider", "provider_repository_id")))}
        output = self._run(context, {"policy_revision": context.revision, "instance_id": instance_id,
            "request": request, "operator_ref": operator_ref, "current": current, "legacy_records": relevant, "now": now.isoformat()})
        try:
            result = json.loads(output)
            if (not isinstance(result, dict) or set(result) != {"record", "readiness", "readiness_digest", "decision_digest", "ai_decision_id"}
                    or result["readiness_digest"] != record_digest(result["readiness"])
                    or result["readiness"].get("policy_ref") != context.policy_ref
                    or result["readiness"].get("outcome") != "allowed" or result["ai_decision_id"] is not None):
                raise ValueError()
            return result
        except (ValueError, TypeError, KeyError) as exc:
            raise InstanceRecordUnavailable("native inventory policy result integrity failed") from exc


class InstanceRepositoryInventoryService(InstanceRepositoryIntakeService):
    """Reuse the same aggregate, locks, CAS, receipts and original-result recovery."""
    def _original(self, name, payload):
        if (not isinstance(payload, dict) or set(payload) != {"schema_version", "request", "operator_ref"}
                or type(payload["schema_version"]) is not int or payload["schema_version"] != 1):
            raise InstanceRecordInvalid("the exact accepted inventory request is required")
        request, operator = payload["request"], payload["operator_ref"]
        if (not isinstance(request, dict) or request.get("instance_id") != self.instance_id
                or request.get("artifact_type") != "instance-repository-inventory-request"
                or not isinstance(request.get("target"), dict) or request["target"].get("name") != name
                or request["target"].get("record_id") != f"repo:{name}" or not isinstance(operator, str)
                or not operator.strip() or len(operator) > 256):
            raise InstanceRecordInvalid("inventory request does not bind this installation and repository")
        return {"request": request, "operator_ref": operator}

    def resolve(self, name, payload):
        original = self._original(name, payload)
        def read(sessions):
            saved = self._store(sessions=sessions).operation(instance_id=self.instance_id,
                idempotency_key=original["request"].get("idempotency_key"))
            if saved and record_digest(saved["command"]["decision"].get("operator_input")) != record_digest(original):
                raise InstanceRecordConflict("original inventory input differs from its saved operation")
            return {"schema_version": 1, "authority": {"kind": "internal-instance-record", "instance_id": self.instance_id},
                    "found": saved is not None, **(saved["result"] if saved else {"record": None, "receipt": None})}
        return self._within_operation(name, payload, read)

    def _accept_locked(self, name, payload, *, actor, sessions):
        original = self._original(name, payload)
        request, operator = original["request"], original["operator_ref"]
        saved = self._store(sessions=sessions).operation(instance_id=self.instance_id, idempotency_key=request.get("idempotency_key"), actor=actor)
        if saved:
            if record_digest(saved["command"]["decision"].get("operator_input")) != record_digest(original):
                raise InstanceRecordConflict("inventory operation already belongs to different accepted input")
            return saved["result"]
        current = self._store(sessions=sessions).read(instance_id=self.instance_id, record_id=f"repo:{name}")
        context, now = self.policy.context(), self.clock()
        plan = self.policy.plan_inventory(context, request=request, instance_id=self.instance_id, operator_ref=operator, current=current, now=now)
        command = {"schema_version": 1, "instance_id": self.instance_id, "record_id": f"repo:{name}",
            "idempotency_key": request["idempotency_key"],
            "expected": {"version": request["expected_state"]["record_version"], "digest": request["expected_state"]["record_digest"]},
            "action": f"inventory.{request['action']}", "record": plan["record"],
            "decision": {"operator_input": original, "readiness": plan["readiness"]}}
        def validate(candidate, observed, authenticated_actor):
            latest = self.policy.context()
            checked = self.policy.plan_inventory(latest, request=request, instance_id=self.instance_id,
                operator_ref=operator, current=observed, now=now)
            if authenticated_actor != actor or candidate != command or checked != plan:
                raise InstanceRecordConflict("inventory changed before transactional acceptance")
            return {"policy_ref": f"git://workspace-governance/{latest.revision}/contracts/instance-operational-records.yaml@{latest.policy_ref['contract_digest']}",
                    "decision_digest": plan["decision_digest"], "readiness_digest": plan["readiness_digest"], "operator_ref": operator}
        return self._store(validate, sessions=sessions).commit(command, actor=actor)


def build_instance_repository_inventory_runtime():
    from .instance_repository_intake import build_instance_repository_intake_runtime
    base = build_instance_repository_intake_runtime()
    return InstanceRepositoryInventoryService(session_factory=base.sessions, instance_id=base.instance_id,
        policy=NativeInstanceInventoryPolicy(base.policy.authority), clock=base.clock)
