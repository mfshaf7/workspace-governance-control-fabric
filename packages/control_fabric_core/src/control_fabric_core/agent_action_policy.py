"""Fail-closed agent-action policy evaluation against pinned workspace authority."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
import yaml

from .canonical_json import canonical_digest, canonicalization_errors
from .validation_execution import LedgerEvent, append_ledger_event


DEFAULT_AGENT_ACTION_CONTRACT_ROOT = (
    Path(__file__).resolve().parents[4] / "contracts" / "agent-action"
)
AGENT_ACTION_CONTRACT_ROOT_ENV = "WGCF_AGENT_ACTION_CONTRACT_ROOT"
AGENT_ACTION_LEDGER_SCHEMA_VERSION = 1
MAX_AGENT_ACTION_EVALUATION_REQUEST_BYTES = 64 * 1024

BASE_OBLIGATIONS = (
    "record-terminal-action-receipt",
    "require-current-source-version",
    "deny-raw-context-projection",
)
MUTATE_OBLIGATIONS = (
    "require-exact-operator-approval",
    "require-owner-receipt-after-invocation",
)


class AgentActionPolicyError(ValueError):
    """Base error for agent-action policy evaluation."""


class AgentActionContractError(AgentActionPolicyError):
    """The pinned contract or supplied request is invalid."""


@dataclass(frozen=True)
class AgentActionContractBundle:
    """Digest-verified runtime snapshot of Workspace Governance authority."""

    authority: dict[str, Any]
    authority_digest: str
    authority_uri: str
    root: Path
    source_commit: str
    validators: dict[str, Draft202012Validator]

    @classmethod
    def load(cls, root: str | Path | None = None) -> "AgentActionContractBundle":
        resolved_root = Path(
            root
            or os.environ.get(AGENT_ACTION_CONTRACT_ROOT_ENV)
            or DEFAULT_AGENT_ACTION_CONTRACT_ROOT,
        ).resolve()
        manifest_path = resolved_root / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise AgentActionContractError(
                "agent-action contract manifest is unavailable or invalid",
            ) from exc

        source = manifest.get("source")
        authority_entry = manifest.get("authority")
        schema_entries = manifest.get("schemas")
        if not isinstance(source, dict) or source.get("repo") != "workspace-governance":
            raise AgentActionContractError("agent-action contract source must be workspace-governance")
        source_commit = _non_empty_string(source.get("commit"))
        if source_commit is None:
            raise AgentActionContractError("agent-action contract source commit is required")
        if not isinstance(authority_entry, dict) or not isinstance(schema_entries, dict):
            raise AgentActionContractError("agent-action contract manifest is incomplete")

        authority_path = _verified_path(resolved_root, authority_entry)
        try:
            authority = yaml.safe_load(authority_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise AgentActionContractError("agent-action authority contract is invalid") from exc
        if not isinstance(authority, dict) or authority.get("owner_repo") != "workspace-governance":
            raise AgentActionContractError("agent-action authority ownership is invalid")
        action_classes = authority.get("action_classes")
        if not isinstance(action_classes, dict) or set(action_classes) != {
            "read",
            "advise",
            "draft",
            "mutate",
        }:
            raise AgentActionContractError("agent-action authority classes are incomplete")

        validators: dict[str, Draft202012Validator] = {}
        for artifact_type in ("agent_action_request", "agent_action_policy_decision"):
            entry = schema_entries.get(artifact_type)
            if not isinstance(entry, dict):
                raise AgentActionContractError(f"schema entry {artifact_type} is missing")
            schema_path = _verified_path(resolved_root, entry)
            try:
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
            except (OSError, json.JSONDecodeError, SchemaError) as exc:
                raise AgentActionContractError(f"schema {artifact_type} is invalid") from exc
            validators[artifact_type] = Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            )
        return cls(
            authority=authority,
            authority_digest=f"sha256:{authority_entry['sha256']}",
            authority_uri=str(authority_entry.get("uri") or "").strip(),
            root=resolved_root,
            source_commit=source_commit,
            validators=validators,
        )

    def validate(self, artifact_type: str, artifact: dict[str, Any]) -> None:
        validator = self.validators.get(artifact_type)
        if validator is None:
            raise AgentActionContractError(f"unsupported agent-action artifact {artifact_type}")
        errors = sorted(validator.iter_errors(artifact), key=lambda error: list(error.absolute_path))
        if errors:
            error = errors[0]
            path = ".".join(str(part) for part in error.absolute_path) or "<root>"
            raise AgentActionContractError(f"{artifact_type} {path}: {error.message}")


@dataclass(frozen=True)
class AgentActionPolicyDecision:
    """Canonical policy-decision artifact issued by WGCF."""

    record: dict[str, Any]

    @property
    def decision_id(self) -> str:
        return str(self.record["decision_id"])

    @property
    def outcome(self) -> str:
        return str(self.record["outcome"])

    def to_record(self) -> dict[str, Any]:
        return copy.deepcopy(self.record)


@dataclass(frozen=True)
class AgentActionEvaluationResult:
    """A policy decision and its append-only audit event."""

    decision: AgentActionPolicyDecision
    ledger_event: LedgerEvent

    def to_record(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_record(),
            "ledger_event": self.ledger_event.to_record(),
        }


def evaluate_agent_action_request(
    request: dict[str, Any],
    *,
    current: dict[str, Any],
    contract_bundle: AgentActionContractBundle | None = None,
    now: datetime | str | None = None,
) -> AgentActionPolicyDecision:
    """Evaluate one exact action request without invoking its owner workflow."""

    bundle = contract_bundle or AgentActionContractBundle.load()
    if not isinstance(request, dict):
        raise AgentActionContractError("agent-action request must be an object")
    if not isinstance(current, dict):
        raise AgentActionContractError("agent-action current bindings must be an object")
    canonical_errors = canonicalization_errors(request)
    if canonical_errors:
        raise AgentActionContractError(canonical_errors[0])
    bundle.validate("agent_action_request", request)
    _validate_content_digest(request, "agent-action request")

    decision_time = _coerce_timestamp(now)
    requested_at = _parse_timestamp(request.get("requested_at"), "requested_at")
    request_expires_at = _parse_timestamp(request.get("expires_at"), "expires_at")
    action_class = str(request["action_class"])
    deny_codes: list[str] = []
    review_codes: list[str] = []

    if requested_at >= request_expires_at:
        deny_codes.append("invalid-request-window")
    elif decision_time < requested_at:
        deny_codes.append("request-not-yet-current")
    elif decision_time >= request_expires_at:
        deny_codes.append("request-expired")

    _compare_binding(
        request["operator"]["principal_id"],
        current.get("operator_principal_id"),
        "operator-identity-unverified",
        "operator-identity-mismatch",
        deny_codes,
        review_codes,
    )
    _compare_binding(
        request["operator"]["session_ref"],
        current.get("operator_session_ref"),
        "operator-session-unverified",
        "operator-session-stale",
        deny_codes,
        review_codes,
    )
    _compare_binding(
        request["operator"]["acceptance_ref"],
        current.get("operator_acceptance_ref"),
        "operator-acceptance-unverified",
        "operator-acceptance-mismatch",
        deny_codes,
        review_codes,
    )
    _compare_binding(
        request["caller"]["workload_id"],
        current.get("caller_workload_id"),
        "caller-identity-unverified",
        "caller-identity-mismatch",
        deny_codes,
        review_codes,
    )
    _compare_binding(
        request["caller"]["credential_binding_ref"],
        current.get("caller_credential_binding_ref"),
        "caller-credential-unverified",
        "caller-credential-stale",
        deny_codes,
        review_codes,
    )
    _compare_binding(
        request["agent"]["instance_id"],
        current.get("agent_instance_id"),
        "agent-instance-unverified",
        "agent-instance-stale",
        deny_codes,
        review_codes,
    )
    _compare_optional_binding(
        request.get("model_invocation_ref"),
        current.get("model_invocation_ref"),
        "model-invocation-unverified",
        "model-invocation-mismatch",
        deny_codes,
        review_codes,
    )

    workflow = request["workflow"]
    _compare_binding(
        workflow["workflow_id"],
        current.get("workflow_id"),
        "workflow-admission-unverified",
        "workflow-not-admitted",
        deny_codes,
        review_codes,
    )
    _compare_binding(
        workflow["workflow_version"],
        current.get("workflow_version"),
        "workflow-version-unverified",
        "workflow-version-mismatch",
        deny_codes,
        review_codes,
    )
    admitted_commands = current.get("admitted_commands")
    if not isinstance(admitted_commands, list):
        review_codes.append("workflow-command-admission-unverified")
    elif workflow["command"] not in admitted_commands:
        deny_codes.append("workflow-command-not-admitted")

    target = request["target"]
    _compare_binding(
        target["owner_repo"],
        current.get("target_owner_repo"),
        "target-owner-unverified",
        "target-owner-mismatch",
        deny_codes,
        review_codes,
    )
    _compare_binding(
        target["resource_id"],
        current.get("target_resource_id"),
        "target-resource-unverified",
        "target-resource-mismatch",
        deny_codes,
        review_codes,
    )
    _compare_binding(
        target["source_version"],
        current.get("source_version"),
        "source-version-unverified",
        "source-version-mismatch",
        deny_codes,
        review_codes,
    )

    context = request["context"]
    _compare_optional_binding(
        context.get("packet_ref"),
        current.get("context_packet_ref"),
        "context-packet-unverified",
        "context-packet-mismatch",
        deny_codes,
        review_codes,
    )
    _compare_optional_binding(
        context.get("receipt_ref"),
        current.get("context_receipt_ref"),
        "context-receipt-unverified",
        "context-receipt-mismatch",
        deny_codes,
        review_codes,
    )

    authority = request["authority"]
    _compare_binding(
        authority["delegation_ref"],
        current.get("delegation_ref"),
        "delegation-unverified",
        "delegation-mismatch",
        deny_codes,
        review_codes,
    )
    _compare_binding(
        authority["policy_profile_ref"],
        current.get("policy_profile_ref"),
        "policy-profile-unverified",
        "policy-profile-mismatch",
        deny_codes,
        review_codes,
    )
    if action_class == "mutate":
        _compare_binding(
            authority["approval_ref"],
            current.get("approval_ref"),
            "approval-unverified",
            "approval-mismatch",
            deny_codes,
            review_codes,
        )
        approval_expires_at = current.get("approval_expires_at")
        if approval_expires_at is None:
            review_codes.append("approval-expiry-unverified")
        else:
            try:
                if decision_time >= _parse_timestamp(approval_expires_at, "approval_expires_at"):
                    deny_codes.append("approval-expired")
            except AgentActionContractError:
                deny_codes.append("approval-expiry-invalid")

    consumed = current.get("consumed_idempotency")
    if not isinstance(consumed, list):
        review_codes.append("idempotency-state-unverified")
    else:
        for entry in consumed:
            if not isinstance(entry, dict) or entry.get("idempotency_key") != request["idempotency_key"]:
                continue
            if entry.get("intent_digest") == request["intent"]["digest"]:
                deny_codes.append("idempotency-key-consumed")
            else:
                deny_codes.append("idempotency-intent-conflict")
            break

    deny_codes = _unique(deny_codes)
    review_codes = _unique(review_codes)
    if deny_codes:
        outcome = "deny"
        reason_codes = deny_codes + review_codes
    elif review_codes:
        outcome = "review-required"
        reason_codes = review_codes
    else:
        outcome = "allow"
        reason_codes = ["authority-bindings-current", "owner-workflow-admitted"]

    obligations = list(BASE_OBLIGATIONS)
    if action_class == "mutate":
        obligations.extend(MUTATE_OBLIGATIONS)
    request_digest = request["integrity"]["content_digest"]
    request_token = str(request["request_id"]).split(":", 1)[-1]
    request_ref = {
        "uri": f"wgcf://agent-actions/requests/{request_token}",
        "digest": request_digest,
    }
    bindings = {
        "operator_principal_id": request["operator"]["principal_id"],
        "operator_session_ref": copy.deepcopy(request["operator"]["session_ref"]),
        "caller_workload_id": request["caller"]["workload_id"],
        "agent_instance_id": request["agent"]["instance_id"],
        "workflow_execution_id": workflow["execution_id"],
        "target_owner_repo": target["owner_repo"],
        "target_resource_id": target["resource_id"],
        "source_version": target["source_version"],
        "approval_ref": copy.deepcopy(authority["approval_ref"]),
    }
    policy_refs = _unique_refs(
        [
            {"uri": bundle.authority_uri, "digest": bundle.authority_digest},
            copy.deepcopy(authority["policy_profile_ref"]),
        ],
    )
    decision_time_text = _format_timestamp(decision_time)
    identity_projection = {
        "request_ref": request_ref,
        "action_class": action_class,
        "outcome": outcome,
        "reason_codes": reason_codes,
        "obligations": obligations,
        "decided_at": decision_time_text,
        "expires_at": request["expires_at"],
        "bindings": bindings,
        "policy_refs": policy_refs,
    }
    decision_digest = canonical_digest(identity_projection).removeprefix("sha256:")
    record = {
        "schema_version": 1,
        "artifact_type": "agent_action_policy_decision",
        "decision_id": f"agent-action-decision:{decision_digest[:24]}",
        **identity_projection,
        "integrity": {
            "canonicalization": "RFC8785",
            "algorithm": "sha256",
            "content_digest": "",
        },
    }
    record["integrity"]["content_digest"] = canonical_digest(_content_projection(record))
    bundle.validate("agent_action_policy_decision", record)
    return AgentActionPolicyDecision(record=record)


def build_agent_action_policy_ledger_event(
    *,
    actor: str,
    decision: AgentActionPolicyDecision,
) -> LedgerEvent:
    """Build the compact append-only event for one issued decision."""

    record = decision.to_record()
    decision_ref = {
        "receipt_id": record["decision_id"],
        "digest": record["integrity"]["content_digest"],
        "outcome": record["outcome"],
    }
    target = f"{record['bindings']['target_owner_repo']}:{record['bindings']['target_resource_id']}"
    event_payload = {
        "action": "agent-action.policy-decision.issued",
        "actor": actor,
        "decision_ref": decision_ref,
        "event_time": record["decided_at"],
        "target": target,
    }
    event_digest = canonical_digest(event_payload).removeprefix("sha256:")
    return LedgerEvent(
        action="agent-action.policy-decision.issued",
        actor=actor,
        artifact_refs=(),
        event_id=f"ledger-event:{event_digest[:24]}",
        event_time=record["decided_at"],
        outcome=record["outcome"],
        receipt_refs=(decision_ref,),
        schema_version=AGENT_ACTION_LEDGER_SCHEMA_VERSION,
        target=target,
    )


def run_agent_action_evaluation(
    request: dict[str, Any],
    *,
    actor: str,
    current: dict[str, Any],
    ledger_path: str | Path,
    contract_bundle: AgentActionContractBundle | None = None,
    now: datetime | str | None = None,
) -> AgentActionEvaluationResult:
    """Issue one decision and append its compact ledger event."""

    decision = evaluate_agent_action_request(
        request,
        current=current,
        contract_bundle=contract_bundle,
        now=now,
    )
    event = build_agent_action_policy_ledger_event(actor=actor, decision=decision)
    append_ledger_event(ledger_path, event)
    return AgentActionEvaluationResult(decision=decision, ledger_event=event)


def _verified_path(root: Path, entry: dict[str, Any]) -> Path:
    relative_path = _non_empty_string(entry.get("path"))
    expected_digest = _non_empty_string(entry.get("sha256"))
    if relative_path is None or expected_digest is None:
        raise AgentActionContractError("agent-action manifest entry is incomplete")
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root):
        raise AgentActionContractError("agent-action contract path escapes its bundle")
    try:
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise AgentActionContractError(f"agent-action contract file {relative_path} is unavailable") from exc
    if actual_digest != expected_digest:
        raise AgentActionContractError(
            f"agent-action contract file {relative_path} does not match its manifest digest",
        )
    return path


def _content_projection(artifact: dict[str, Any]) -> dict[str, Any]:
    projection = copy.deepcopy(artifact)
    integrity = projection.get("integrity")
    if isinstance(integrity, dict):
        integrity.pop("content_digest", None)
    return projection


def _validate_content_digest(artifact: dict[str, Any], label: str) -> None:
    actual = artifact.get("integrity", {}).get("content_digest")
    expected = canonical_digest(_content_projection(artifact))
    if actual != expected:
        raise AgentActionContractError(f"{label} integrity.content_digest does not match canonical content")


def _compare_binding(
    requested: Any,
    current: Any,
    unverified_code: str,
    mismatch_code: str,
    deny_codes: list[str],
    review_codes: list[str],
) -> None:
    if current is None:
        review_codes.append(unverified_code)
    elif current != requested:
        deny_codes.append(mismatch_code)


def _compare_optional_binding(
    requested: Any,
    current: Any,
    unverified_code: str,
    mismatch_code: str,
    deny_codes: list[str],
    review_codes: list[str],
) -> None:
    if requested is None:
        return
    _compare_binding(
        requested,
        current,
        unverified_code,
        mismatch_code,
        deny_codes,
        review_codes,
    )


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise AgentActionContractError(f"{field_name} must be a date-time string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentActionContractError(f"{field_name} must be a valid date-time") from exc
    if parsed.tzinfo is None:
        raise AgentActionContractError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC)


def _coerce_timestamp(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise AgentActionContractError("decision time must include a timezone")
        return value.astimezone(UTC)
    return _parse_timestamp(value, "decision time")


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _non_empty_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _unique_refs(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        key = canonical_digest(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
