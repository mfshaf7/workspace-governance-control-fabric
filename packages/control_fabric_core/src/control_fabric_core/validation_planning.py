"""Deterministic validation planning primitives.

The planner selects declared validators from a runtime governance manifest. It
does not execute commands, persist receipts, or invent workspace policy.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from hashlib import sha256
from typing import Any

from .graph_ingestion import build_manifest_graph
from .manifests import validate_governance_manifest


class ValidationTier(StrEnum):
    """Validation intensity requested by an operator or automation surface."""

    SMOKE = "smoke"
    SCOPED = "scoped"
    FULL = "full"
    RELEASE = "release"


class ValidationCheckType(StrEnum):
    """Execution family for a declared validator."""

    COMMAND = "command"
    CONTRACT = "contract"
    PROJECTION = "projection"


VALIDATION_TIER_ORDER = {
    ValidationTier.SMOKE: 10,
    ValidationTier.SCOPED: 20,
    ValidationTier.FULL: 30,
    ValidationTier.RELEASE: 40,
}

DEFAULT_VALIDATION_TIER = ValidationTier.SCOPED
DEFAULT_CHECK_TYPE = ValidationCheckType.COMMAND
KNOWN_TARGET_PREFIXES = ("authority:", "component:", "projection:", "repo:", "validator:", "art:")


@dataclass(frozen=True)
class ValidationTarget:
    """Normalized planner target."""

    scope: str
    target_type: str
    target_id: str

    def to_record(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationCheck:
    """One selected validator command or contract check in a plan."""

    check_id: str
    check_type: str
    command: str
    owner_repo: str
    required: bool
    scopes: tuple[str, ...]
    tier: str
    validator_id: str
    reason: str

    def to_record(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "scopes": list(self.scopes),
        }


@dataclass(frozen=True)
class SuppressedValidator:
    """Validator that was deliberately left out of a plan."""

    validator_id: str
    reason: str

    def to_record(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class PlannerDecision:
    """Planner outcome and operator-readable reasoning."""

    blocked_reasons: tuple[str, ...]
    outcome: str
    reasons: tuple[str, ...]
    requires_operator_review: bool
    suppressed_validators: tuple[SuppressedValidator, ...]

    def to_record(self) -> dict[str, Any]:
        return {
            "blocked_reasons": list(self.blocked_reasons),
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "requires_operator_review": self.requires_operator_review,
            "suppressed_validators": [
                validator.to_record()
                for validator in self.suppressed_validators
            ],
        }


@dataclass(frozen=True)
class ValidationPlan:
    """Operator-safe validation plan record."""

    checks: tuple[ValidationCheck, ...]
    decision: PlannerDecision
    manifest_id: str
    plan_id: str
    target: ValidationTarget
    tier: str

    def to_record(self) -> dict[str, Any]:
        return {
            "checks": [check.to_record() for check in self.checks],
            "decision": self.decision.to_record(),
            "manifest_id": self.manifest_id,
            "plan_id": self.plan_id,
            "target": self.target.to_record(),
            "tier": self.tier,
        }


def build_validation_plan(
    manifest: dict[str, Any],
    target_scope: str,
    tier: str | ValidationTier = ValidationTier.SCOPED,
) -> ValidationPlan:
    """Build a deterministic validation plan from manifest-declared validators."""

    validation = validate_governance_manifest(manifest)
    if not validation.valid:
        raise ValueError(f"manifest is invalid: {'; '.join(validation.errors)}")

    requested_tier = _coerce_tier(tier)
    target = normalize_validation_target(target_scope)
    graph = build_manifest_graph(manifest)
    declared_scope_ids = _declared_scope_ids(manifest, graph)
    target_declared = target.scope in declared_scope_ids or target.target_type == "workspace"

    selected: list[ValidationCheck] = []
    suppressed: list[SuppressedValidator] = []
    for validator in sorted(manifest["validators"], key=lambda item: item["validator_id"]):
        validator_tier = _validator_tier(validator)
        if not _tier_included(validator_tier, requested_tier):
            suppressed.append(
                SuppressedValidator(
                    validator_id=validator["validator_id"],
                    reason=f"validator tier {validator_tier.value} is above requested tier {requested_tier.value}",
                ),
            )
            continue
        if not _scope_included(validator["scopes"], target, requested_tier):
            suppressed.append(
                SuppressedValidator(
                    validator_id=validator["validator_id"],
                    reason=f"validator does not declare target scope {target.scope}",
                ),
            )
            continue

        selected.append(_validation_check(validator, target, validator_tier, requested_tier))

    reasons = _decision_reasons(target, requested_tier, selected, target_declared)
    blocked_reasons = _blocked_reasons(manifest, requested_tier)
    if blocked_reasons:
        outcome = "blocked"
        requires_operator_review = True
    elif selected:
        outcome = "planned"
        requires_operator_review = False
    else:
        outcome = "no_matching_validators"
        requires_operator_review = True

    decision = PlannerDecision(
        blocked_reasons=tuple(blocked_reasons),
        outcome=outcome,
        reasons=tuple(reasons),
        requires_operator_review=requires_operator_review,
        suppressed_validators=tuple(suppressed),
    )
    plan_id = _plan_id(manifest["manifest_id"], target.scope, requested_tier, selected, decision)
    return ValidationPlan(
        checks=tuple(selected),
        decision=decision,
        manifest_id=manifest["manifest_id"],
        plan_id=plan_id,
        target=target,
        tier=requested_tier.value,
    )


def normalize_validation_target(target_scope: str) -> ValidationTarget:
    """Normalize and validate a planner target scope."""

    scope = target_scope.strip()
    if not scope:
        raise ValueError("validation target scope must not be empty")
    if scope == "workspace":
        return ValidationTarget(scope=scope, target_type="workspace", target_id="workspace")
    for prefix in KNOWN_TARGET_PREFIXES:
        if scope.startswith(prefix):
            target_id = scope.removeprefix(prefix).strip()
            if not target_id:
                raise ValueError(f"validation target {prefix} must include an id")
            return ValidationTarget(
                scope=scope,
                target_type=prefix.removesuffix(":"),
                target_id=target_id,
            )
    raise ValueError(
        "validation target scope must be workspace or start with one of: "
        + ", ".join(KNOWN_TARGET_PREFIXES),
    )


def _validation_check(
    validator: dict[str, Any],
    target: ValidationTarget,
    validator_tier: ValidationTier,
    requested_tier: ValidationTier,
) -> ValidationCheck:
    validator_id = validator["validator_id"].strip()
    return ValidationCheck(
        check_id=_check_id(validator_id, target.scope, requested_tier),
        check_type=_validator_check_type(validator).value,
        command=validator["command"].strip(),
        owner_repo=validator["owner_repo"].strip(),
        reason=_selection_reason(validator, target, validator_tier, requested_tier),
        required=bool(validator.get("required", True)),
        scopes=tuple(sorted(str(scope).strip() for scope in validator["scopes"])),
        tier=validator_tier.value,
        validator_id=validator_id,
    )


def _coerce_tier(tier: str | ValidationTier) -> ValidationTier:
    if isinstance(tier, ValidationTier):
        return tier
    normalized = str(tier).strip().lower()
    try:
        return ValidationTier(normalized)
    except ValueError as error:
        allowed = ", ".join(item.value for item in ValidationTier)
        raise ValueError(f"unknown validation tier {tier!r}; expected one of: {allowed}") from error


def _validator_tier(validator: dict[str, Any]) -> ValidationTier:
    value = validator.get("validation_tier") or validator.get("tier") or DEFAULT_VALIDATION_TIER.value
    return _coerce_tier(str(value))


def _validator_check_type(validator: dict[str, Any]) -> ValidationCheckType:
    value = str(validator.get("check_type") or DEFAULT_CHECK_TYPE.value).strip().lower()
    try:
        return ValidationCheckType(value)
    except ValueError as error:
        allowed = ", ".join(item.value for item in ValidationCheckType)
        raise ValueError(
            f"unknown validation check_type {value!r} for {validator['validator_id']}; expected one of: {allowed}",
        ) from error


def _tier_included(validator_tier: ValidationTier, requested_tier: ValidationTier) -> bool:
    return VALIDATION_TIER_ORDER[validator_tier] <= VALIDATION_TIER_ORDER[requested_tier]


def _scope_included(
    validator_scopes: list[str],
    target: ValidationTarget,
    requested_tier: ValidationTier,
) -> bool:
    if requested_tier in {ValidationTier.FULL, ValidationTier.RELEASE}:
        return True
    declared_scopes = {str(scope).strip() for scope in validator_scopes}
    return target.scope in declared_scopes or "workspace" in declared_scopes


def _selection_reason(
    validator: dict[str, Any],
    target: ValidationTarget,
    validator_tier: ValidationTier,
    requested_tier: ValidationTier,
) -> str:
    declared_scopes = {str(scope).strip() for scope in validator["scopes"]}
    if target.scope in declared_scopes or "workspace" in declared_scopes:
        return f"validator declares scope for {target.scope} at {validator_tier.value} tier"
    if requested_tier in {ValidationTier.FULL, ValidationTier.RELEASE}:
        return f"{requested_tier.value} tier includes every manifest-declared validator"
    return f"validator selected for {target.scope}"


def _decision_reasons(
    target: ValidationTarget,
    requested_tier: ValidationTier,
    checks: list[ValidationCheck],
    target_declared: bool,
) -> list[str]:
    reasons = [
        f"target={target.scope}",
        f"tier={requested_tier.value}",
        f"selected_checks={len(checks)}",
    ]
    if not target_declared:
        reasons.append("target scope is not declared by the manifest graph")
    if requested_tier in {ValidationTier.FULL, ValidationTier.RELEASE}:
        reasons.append("full-surface tier includes every manifest-declared validator")
    return reasons


def _blocked_reasons(manifest: dict[str, Any], requested_tier: ValidationTier) -> list[str]:
    if requested_tier is not ValidationTier.RELEASE:
        return []
    stale_authorities = [
        authority_ref["authority_id"]
        for authority_ref in manifest["authority_refs"]
        if str(authority_ref.get("freshness_status") or "unknown").strip().lower() != "current"
    ]
    if not stale_authorities:
        return []
    return [
        "release tier requires current authority refs: "
        + ", ".join(sorted(stale_authorities)),
    ]


def _declared_scope_ids(manifest: dict[str, Any], graph) -> set[str]:
    scopes = {node.node_id for node in graph.nodes}
    for validator in manifest["validators"]:
        scopes.update(str(scope).strip() for scope in validator["scopes"])
    return scopes


def _check_id(validator_id: str, target_scope: str, tier: ValidationTier) -> str:
    digest = sha256(f"{validator_id}|{target_scope}|{tier.value}".encode("utf-8")).hexdigest()[:16]
    return f"check:{digest}"


def _plan_id(
    manifest_id: str,
    target_scope: str,
    tier: ValidationTier,
    checks: list[ValidationCheck],
    decision: PlannerDecision,
) -> str:
    payload = {
        "blocked_reasons": list(decision.blocked_reasons),
        "check_ids": [check.check_id for check in checks],
        "manifest_id": manifest_id,
        "target_scope": target_scope,
        "tier": tier.value,
    }
    digest = sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    return f"validation-plan:{digest}"
