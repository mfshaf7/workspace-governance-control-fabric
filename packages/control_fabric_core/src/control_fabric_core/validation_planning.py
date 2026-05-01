"""Deterministic validation planning primitives.

The planner selects declared validators from a runtime governance manifest. It
does not execute commands, persist receipts, or invent workspace policy.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any

from .graph_ingestion import build_manifest_graph
from .manifests import validate_governance_manifest
from .performance_budgets import evaluate_validation_plan_budget


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
KNOWN_TARGET_PREFIXES = (
    "authority:",
    "component:",
    "projection:",
    "profile:",
    "repo:",
    "validator:",
    "art:",
    "release:",
    "changed-file:",
)


class ValidationExecutionMode(StrEnum):
    """Whether the planned check should run or can reuse a fresh receipt."""

    RUN = "run"
    SKIP_FRESH_RECEIPT = "skip_fresh_receipt"


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

    cache_decision: dict[str, Any]
    check_id: str
    check_type: str
    command: str
    execution_mode: str
    execution_policy: dict[str, Any]
    owner_repo: str
    receipt_digest: str | None
    receipt_id: str | None
    required: bool
    reuse_reason: str | None
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
class ValidationCheckStatus:
    """Operator-readable status for one manifest-declared validator."""

    owner_repo: str
    reason: str
    required: bool
    status: str
    validator_id: str

    def to_record(self) -> dict[str, Any]:
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

    check_statuses: tuple[ValidationCheckStatus, ...]
    checks: tuple[ValidationCheck, ...]
    decision: PlannerDecision
    manifest_id: str
    performance_budget: dict[str, Any]
    plan_id: str
    target: ValidationTarget
    tier: str

    def to_record(self) -> dict[str, Any]:
        return {
            "check_statuses": [status.to_record() for status in self.check_statuses],
            "checks": [check.to_record() for check in self.checks],
            "decision": self.decision.to_record(),
            "manifest_id": self.manifest_id,
            "performance_budget": self.performance_budget,
            "plan_id": self.plan_id,
            "target": self.target.to_record(),
            "tier": self.tier,
        }


def build_validation_plan(
    manifest: dict[str, Any],
    target_scope: str,
    tier: str | ValidationTier = ValidationTier.SCOPED,
    receipts: list[dict[str, Any]] | None = None,
    waivers: list[dict[str, Any]] | None = None,
    now: datetime | str | None = None,
) -> ValidationPlan:
    """Build a deterministic validation plan from manifest-declared validators."""

    validation = validate_governance_manifest(manifest)
    if not validation.valid:
        raise ValueError(f"manifest is invalid: {'; '.join(validation.errors)}")

    requested_tier = _coerce_tier(tier)
    receipt_records = tuple(receipts or [])
    waiver_records = tuple(waivers or [])
    planning_time = _coerce_now(now)
    target = normalize_validation_target(target_scope)
    graph = build_manifest_graph(manifest)
    declared_scope_ids = _declared_scope_ids(manifest, graph)
    target_scopes = _target_scope_candidates(manifest, target)
    target_declared = bool(target_scopes & declared_scope_ids) or target.target_type == "workspace"

    selected: list[ValidationCheck] = []
    suppressed: list[SuppressedValidator] = []
    check_statuses: list[ValidationCheckStatus] = []
    for validator in sorted(manifest["validators"], key=lambda item: item["validator_id"]):
        validator_tier = _validator_tier(validator)
        if not _tier_included(validator_tier, requested_tier):
            reason = f"validator tier {validator_tier.value} is above requested tier {requested_tier.value}"
            suppressed.append(SuppressedValidator(validator_id=validator["validator_id"], reason=reason))
            check_statuses.append(
                _check_status(validator, "suppressed", reason),
            )
            continue
        if not _scope_included(validator["scopes"], target, requested_tier, manifest):
            reason = f"validator does not declare a scope matching {target.scope}"
            suppressed.append(SuppressedValidator(validator_id=validator["validator_id"], reason=reason))
            check_statuses.append(
                _check_status(validator, "suppressed", reason),
            )
            continue

        waiver = _valid_validator_waiver(validator, waiver_records, planning_time)
        if waiver:
            check_statuses.append(
                _check_status(
                    validator,
                    "waived",
                    f"validator is covered by waiver {waiver['waiver_id']}",
                ),
            )
            continue

        selected_check = _validation_check(
            validator,
            target,
            validator_tier,
            requested_tier,
            manifest,
            receipt_records,
            planning_time,
        )
        selected.append(selected_check)
        check_statuses.append(_selected_check_status(selected_check, manifest))

    reasons = _decision_reasons(
        manifest,
        target,
        requested_tier,
        selected,
        target_declared,
    )
    blocked_reasons = _blocked_reasons(manifest, requested_tier)
    if blocked_reasons:
        outcome = "blocked"
        requires_operator_review = True
        check_statuses = [
            _replace_status(status, "blocked", "; ".join(blocked_reasons))
            if status.status in {"selected", "stale", "failed", "external-owner-required"}
            else status
            for status in check_statuses
        ]
    elif selected or any(status.status == "waived" for status in check_statuses):
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
    performance_budget = evaluate_validation_plan_budget(
        selected_check_count=len(selected),
        tier=requested_tier.value,
    ).to_record()
    plan_id = _plan_id(
        manifest["manifest_id"],
        target.scope,
        requested_tier,
        selected,
        decision,
        check_statuses,
        performance_budget,
    )
    return ValidationPlan(
        check_statuses=tuple(check_statuses),
        checks=tuple(selected),
        decision=decision,
        manifest_id=manifest["manifest_id"],
        performance_budget=performance_budget,
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
    if scope.startswith("changed-file:"):
        return _normalize_changed_file_target(scope)
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


def validation_target_scope_candidates(
    manifest: dict[str, Any],
    target_scope: str,
) -> tuple[str, ...]:
    """Return deterministic scopes that may satisfy a validation target."""

    validation = validate_governance_manifest(manifest)
    if not validation.valid:
        raise ValueError(f"manifest is invalid: {'; '.join(validation.errors)}")
    target = normalize_validation_target(target_scope)
    return tuple(sorted(_target_scope_candidates(manifest, target)))


def _validation_check(
    validator: dict[str, Any],
    target: ValidationTarget,
    validator_tier: ValidationTier,
    requested_tier: ValidationTier,
    manifest: dict[str, Any],
    receipts: tuple[dict[str, Any], ...],
    now: datetime,
) -> ValidationCheck:
    validator_id = validator["validator_id"].strip()
    execution_mode, receipt_id, receipt_digest, reuse_reason, cache_decision = _receipt_reuse_decision(
        validator,
        target,
        requested_tier,
        receipts,
        now,
        manifest,
    )
    return ValidationCheck(
        cache_decision=cache_decision,
        check_id=_check_id(validator_id, target.scope, requested_tier),
        check_type=_validator_check_type(validator).value,
        command=validator["command"].strip(),
        execution_mode=execution_mode.value,
        execution_policy=_validator_execution_policy(validator),
        owner_repo=validator["owner_repo"].strip(),
        reason=_selection_reason(validator, target, validator_tier, requested_tier, manifest),
        receipt_digest=receipt_digest,
        receipt_id=receipt_id,
        required=bool(validator.get("required", True)),
        reuse_reason=reuse_reason,
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
    manifest: dict[str, Any],
) -> bool:
    if requested_tier in {ValidationTier.FULL, ValidationTier.RELEASE}:
        return True
    declared_scopes = {str(scope).strip() for scope in validator_scopes}
    return bool(declared_scopes & _target_scope_candidates(manifest, target)) or "workspace" in declared_scopes


def _selection_reason(
    validator: dict[str, Any],
    target: ValidationTarget,
    validator_tier: ValidationTier,
    requested_tier: ValidationTier,
    manifest: dict[str, Any],
) -> str:
    declared_scopes = {str(scope).strip() for scope in validator["scopes"]}
    matching_scopes = sorted(declared_scopes & _target_scope_candidates(manifest, target))
    if matching_scopes or "workspace" in declared_scopes:
        if target.target_type == "changed-file" and matching_scopes:
            return (
                f"validator declares scope {matching_scopes[0]} matching "
                f"{target.scope} at {validator_tier.value} tier"
            )
        return f"validator declares scope for {target.scope} at {validator_tier.value} tier"
    if requested_tier in {ValidationTier.FULL, ValidationTier.RELEASE}:
        return f"{requested_tier.value} tier includes every manifest-declared validator"
    return f"validator selected for {target.scope}"


def _check_status(
    validator: dict[str, Any],
    status: str,
    reason: str,
) -> ValidationCheckStatus:
    return ValidationCheckStatus(
        owner_repo=str(validator.get("owner_repo") or "").strip(),
        reason=reason,
        required=bool(validator.get("required", True)),
        status=status,
        validator_id=str(validator.get("validator_id") or "").strip(),
    )


def _replace_status(
    status: ValidationCheckStatus,
    new_status: str,
    reason: str,
) -> ValidationCheckStatus:
    return ValidationCheckStatus(
        owner_repo=status.owner_repo,
        reason=reason,
        required=status.required,
        status=new_status,
        validator_id=status.validator_id,
    )


def _selected_check_status(
    check: ValidationCheck,
    manifest: dict[str, Any],
) -> ValidationCheckStatus:
    reason_code = str(check.cache_decision.get("reason_code") or "")
    if reason_code in {"authority-digest-mismatch", "receipt-stale"}:
        status = "stale"
    elif reason_code == "receipt-failed":
        status = "failed"
    elif check.owner_repo != str(manifest.get("owner_repo") or "").strip():
        status = "external-owner-required"
    else:
        status = "selected"
    return ValidationCheckStatus(
        owner_repo=check.owner_repo,
        reason=str(check.cache_decision.get("reason") or check.reason),
        required=check.required,
        status=status,
        validator_id=check.validator_id,
    )


def _valid_validator_waiver(
    validator: dict[str, Any],
    waivers: tuple[dict[str, Any], ...],
    now: datetime,
) -> dict[str, Any] | None:
    validator_id = str(validator.get("validator_id") or "").strip()
    for waiver in sorted(waivers, key=lambda item: str(item.get("waiver_id") or "")):
        if str(waiver.get("validator_id") or "").strip() != validator_id:
            continue
        waiver_id = _optional_str(waiver.get("waiver_id"))
        if not waiver_id:
            continue
        status = str(waiver.get("status") or "approved").strip().lower()
        if status not in {"active", "approved", "current"}:
            continue
        expires_at = _parse_timestamp(waiver.get("expires_at"))
        if expires_at is not None and expires_at < now:
            continue
        return {
            "expires_at": expires_at.isoformat().replace("+00:00", "Z") if expires_at else None,
            "waiver_id": waiver_id,
        }
    return None


def _decision_reasons(
    manifest: dict[str, Any],
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
    reused_count = sum(
        1
        for check in checks
        if check.execution_mode == ValidationExecutionMode.SKIP_FRESH_RECEIPT.value
    )
    if reused_count:
        reasons.append(f"fresh_receipts_applied={reused_count}")
    if target.target_type == "changed-file":
        expanded_scopes = sorted(_target_scope_candidates(manifest, target) - {target.scope})
        if expanded_scopes:
            reasons.append("changed-file target expands to " + ", ".join(expanded_scopes))
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
    for repo in manifest.get("repos", []):
        scopes.update(_impact_scopes(repo))
    for component in manifest.get("components", []):
        scopes.update(_impact_scopes(component))
    for validator in manifest["validators"]:
        scopes.update(str(scope).strip() for scope in validator["scopes"])
    return scopes


def _normalize_changed_file_target(scope: str) -> ValidationTarget:
    raw_path = scope.removeprefix("changed-file:").strip().replace("\\", "/")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or not raw_path or ".." in path.parts:
        raise ValueError("validation target changed-file: must include a relative repo path")
    normalized_path = str(path)
    if not normalized_path or normalized_path == ".":
        raise ValueError("validation target changed-file: must include a relative repo path")
    return ValidationTarget(
        scope=f"changed-file:{normalized_path}",
        target_type="changed-file",
        target_id=normalized_path,
    )


def _target_scope_candidates(manifest: dict[str, Any], target: ValidationTarget) -> set[str]:
    scopes = {target.scope}
    if target.target_type != "changed-file":
        return scopes

    changed_path = target.target_id
    repos = [
        repo
        for repo in manifest.get("repos", [])
        if str(repo.get("repo_id") or "").strip()
    ]
    for repo in repos:
        source_paths = _source_paths(repo)
        if len(repos) == 1 or _path_matches_source_paths(changed_path, source_paths):
            scopes.add(f"repo:{repo['repo_id'].strip()}")
            scopes.update(_impact_scopes(repo))
    for component in manifest.get("components", []):
        component_id = str(component.get("component_id") or "").strip()
        if not component_id:
            continue
        if _path_matches_source_paths(changed_path, _source_paths(component)):
            scopes.add(f"component:{component_id}")
            scopes.update(_impact_scopes(component))
    profile_scope = _profile_scope_for_path(changed_path)
    if profile_scope:
        scopes.add(profile_scope)
    return scopes


def _source_paths(item: dict[str, Any]) -> tuple[str, ...]:
    raw_paths = item.get("source_paths") or item.get("paths") or []
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    return tuple(
        str(PurePosixPath(str(path).strip().replace("\\", "/")))
        for path in raw_paths
        if str(path).strip()
    )


def _impact_scopes(item: dict[str, Any]) -> set[str]:
    raw_scopes = item.get("impact_scopes") or item.get("validation_scopes") or []
    if isinstance(raw_scopes, str):
        raw_scopes = [raw_scopes]
    return {
        str(scope).strip()
        for scope in raw_scopes
        if _is_supported_scope(str(scope).strip())
    }


def _path_matches_source_paths(changed_path: str, source_paths: tuple[str, ...]) -> bool:
    if not source_paths:
        return False
    for source_path in source_paths:
        if source_path in {"", "."}:
            return True
        if changed_path == source_path or changed_path.startswith(f"{source_path.rstrip('/')}/"):
            return True
    return False


def _profile_scope_for_path(changed_path: str) -> str | None:
    parts = PurePosixPath(changed_path).parts
    for index in range(len(parts) - 2):
        if parts[index:index + 2] == ("dev-integration", "profiles"):
            profile_id = parts[index + 2].strip()
            if profile_id:
                return f"profile:{profile_id}"
    return None


def _is_supported_scope(scope: str) -> bool:
    if scope == "workspace":
        return True
    return any(scope.startswith(prefix) and scope.removeprefix(prefix).strip() for prefix in KNOWN_TARGET_PREFIXES)


def _receipt_reuse_decision(
    validator: dict[str, Any],
    target: ValidationTarget,
    requested_tier: ValidationTier,
    receipts: tuple[dict[str, Any], ...],
    now: datetime,
    manifest: dict[str, Any],
) -> tuple[ValidationExecutionMode, str | None, str | None, str | None, dict[str, Any]]:
    if not _validator_allows_receipt_reuse(validator):
        return (
            ValidationExecutionMode.RUN,
            None,
            None,
            None,
            {
                "action": "run",
                "reason": "validator reuse_policy.safe_to_reuse is not true",
            },
        )

    validator_id = validator["validator_id"].strip()
    rejected_receipts: list[dict[str, Any]] = []
    for receipt in _newest_receipts_first(receipts):
        matches, rejection = _receipt_match_decision(
            receipt,
            validator_id,
            target,
            requested_tier,
            validator,
            now,
            manifest,
        )
        if not matches:
            rejected_receipts.append(rejection)
            continue
        receipt_id = str(receipt.get("receipt_id") or "").strip()
        receipt_digest = _optional_str(receipt.get("digest") or receipt.get("receipt_digest"))
        return (
            ValidationExecutionMode.SKIP_FRESH_RECEIPT,
            receipt_id,
            receipt_digest,
            f"fresh successful receipt {receipt_id} covers {target.scope}",
            {
                "action": "reuse",
                "freshness_seconds": _reuse_freshness_seconds(validator, receipt),
                "invalidate_on_authority_change": _invalidate_on_authority_change(validator),
                "reason": f"fresh successful receipt {receipt_id} covers {target.scope}",
                "reason_code": "fresh-receipt-reused",
                "receipt_id": receipt_id,
            },
        )

    primary_rejection = next(
        (
            rejection
            for rejection in rejected_receipts
            if rejection.get("code") != "validator-mismatch"
        ),
        rejected_receipts[0] if rejected_receipts else {},
    )
    return (
        ValidationExecutionMode.RUN,
        None,
        None,
        None,
        {
            "action": "run",
            "invalidate_on_authority_change": _invalidate_on_authority_change(validator),
            "reason": str(primary_rejection.get("reason") or "no reusable receipt supplied"),
            "reason_code": str(primary_rejection.get("code") or "no-reusable-receipt"),
            "rejected_receipt_count": len(rejected_receipts),
            "rejected_receipts": rejected_receipts[:5],
        },
    )


def _validator_allows_receipt_reuse(validator: dict[str, Any]) -> bool:
    reuse_policy = validator.get("reuse_policy")
    if not isinstance(reuse_policy, dict):
        return False
    return bool(reuse_policy.get("safe_to_reuse", False))


def _receipt_match_decision(
    receipt: dict[str, Any],
    validator_id: str,
    target: ValidationTarget,
    requested_tier: ValidationTier,
    validator: dict[str, Any],
    now: datetime,
    manifest: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    receipt_id = str(receipt.get("receipt_id") or "<unknown>").strip()
    if str(receipt.get("validator_id") or "").strip() != validator_id:
        return False, _receipt_rejection(receipt_id, "validator-mismatch", "validator mismatch")
    if str(receipt.get("status") or "").strip().lower() not in {"success", "passed", "valid"}:
        return False, _receipt_rejection(receipt_id, "receipt-failed", "receipt is not successful")
    receipt_target = str(receipt.get("target_scope") or "").strip()
    receipt_scopes = {str(scope).strip() for scope in receipt.get("scopes") or []}
    target_candidates = _target_scope_candidates(manifest, target)
    if (
        target.scope != receipt_target
        and receipt_target not in target_candidates
        and target.scope not in receipt_scopes
        and not (target_candidates & receipt_scopes)
    ):
        return False, _receipt_rejection(receipt_id, "scope-mismatch", "scope mismatch")
    try:
        receipt_tier = _coerce_tier(str(receipt.get("tier") or DEFAULT_VALIDATION_TIER.value))
    except ValueError:
        return False, _receipt_rejection(receipt_id, "invalid-receipt-tier", "invalid receipt tier")
    if VALIDATION_TIER_ORDER[receipt_tier] < VALIDATION_TIER_ORDER[requested_tier]:
        return False, _receipt_rejection(
            receipt_id,
            "tier-mismatch",
            f"receipt tier {receipt_tier.value} is below requested tier {requested_tier.value}",
        )
    if not _receipt_authority_refs_current(receipt, validator, manifest):
        return False, _receipt_rejection(
            receipt_id,
            "authority-digest-mismatch",
            "authority digest changed or missing",
        )
    if not _receipt_is_fresh(receipt, validator, now):
        return False, _receipt_rejection(receipt_id, "receipt-stale", "receipt is stale")
    return True, {"code": "receipt-reusable", "reason": "receipt is reusable", "receipt_id": receipt_id}


def _receipt_rejection(receipt_id: str, code: str, reason: str) -> dict[str, Any]:
    return {
        "code": code,
        "reason": f"{receipt_id}: {reason}",
        "receipt_id": receipt_id,
    }


def _newest_receipts_first(receipts: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    return tuple(
        sorted(
            receipts,
            key=lambda item: (
                _parse_timestamp(item.get("captured_at")) or datetime.min.replace(tzinfo=UTC),
                str(item.get("receipt_id") or ""),
            ),
            reverse=True,
        ),
    )


def _receipt_authority_refs_current(
    receipt: dict[str, Any],
    validator: dict[str, Any],
    manifest: dict[str, Any],
) -> bool:
    if not _invalidate_on_authority_change(validator):
        return True
    expected = _validator_authority_digests(validator, manifest)
    if not expected:
        return True
    supplied = receipt.get("authority_ref_digests")
    if not isinstance(supplied, dict):
        return False
    return all(str(supplied.get(authority_id) or "").strip() == digest for authority_id, digest in expected.items())


def _validator_authority_digests(
    validator: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, str]:
    authority_ids = {
        str(authority_id).strip()
        for authority_id in validator.get("authority_ref_ids", [])
        if str(authority_id).strip()
    }
    digests: dict[str, str] = {}
    for authority_ref in manifest.get("authority_refs", []):
        authority_id = str(authority_ref.get("authority_id") or "").strip()
        digest = _optional_str(authority_ref.get("digest"))
        if authority_id in authority_ids and digest:
            digests[authority_id] = digest
    return digests


def _invalidate_on_authority_change(validator: dict[str, Any]) -> bool:
    reuse_policy = validator.get("reuse_policy")
    if not isinstance(reuse_policy, dict):
        return True
    return bool(reuse_policy.get("invalidate_on_authority_change", True))


def _validator_execution_policy(validator: dict[str, Any]) -> dict[str, Any]:
    raw_policy = validator.get("execution_policy")
    if not isinstance(raw_policy, dict):
        return {}
    policy: dict[str, Any] = {}
    timeout_seconds = _bounded_int(raw_policy.get("timeout_seconds"), minimum=1)
    if timeout_seconds is not None:
        policy["timeout_seconds"] = timeout_seconds
    retry_count = _bounded_int(raw_policy.get("retry_count"), minimum=0)
    if retry_count is not None:
        policy["retry_count"] = retry_count
    output_budget_bytes = _bounded_int(raw_policy.get("output_budget_bytes"), minimum=0)
    if output_budget_bytes is not None:
        policy["output_budget_bytes"] = output_budget_bytes
    max_duration_ms = _bounded_int(raw_policy.get("max_duration_ms"), minimum=1)
    if max_duration_ms is not None:
        policy["max_duration_ms"] = max_duration_ms
    if "fail_on_output_budget_exceeded" in raw_policy:
        policy["fail_on_output_budget_exceeded"] = bool(raw_policy["fail_on_output_budget_exceeded"])
    if "operator_approved" in raw_policy:
        policy["operator_approved"] = bool(raw_policy["operator_approved"])
    for field_name in (
        "invocation_class",
        "profile",
        "safety_class",
        "self_validation_role",
        "working_directory",
    ):
        text_value = _optional_str(raw_policy.get(field_name))
        if text_value:
            policy[field_name] = text_value
    if "prefer_current_python" in raw_policy:
        policy["prefer_current_python"] = bool(raw_policy["prefer_current_python"])
    for field_name in ("allowed_env_vars", "allowed_executables", "allowed_roots", "blocked_env_vars"):
        list_value = _string_list(raw_policy.get(field_name))
        if list_value:
            policy[field_name] = list_value
    return policy


def _bounded_int(value: Any, *, minimum: int) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= minimum else None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def _receipt_is_fresh(receipt: dict[str, Any], validator: dict[str, Any], now: datetime) -> bool:
    expires_at = _parse_timestamp(receipt.get("expires_at"))
    if expires_at is not None:
        return expires_at >= now

    captured_at = _parse_timestamp(receipt.get("captured_at"))
    if captured_at is None:
        return False
    freshness_seconds = _reuse_freshness_seconds(validator, receipt)
    return captured_at + timedelta(seconds=freshness_seconds) >= now


def _reuse_freshness_seconds(validator: dict[str, Any], receipt: dict[str, Any]) -> int:
    reuse_policy = validator.get("reuse_policy")
    raw_value: Any = None
    if isinstance(reuse_policy, dict):
        raw_value = reuse_policy.get("freshness_seconds")
    raw_value = raw_value if raw_value is not None else receipt.get("freshness_seconds")
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _coerce_now(now: datetime | str | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=UTC)
    parsed = _parse_timestamp(now)
    if parsed is None:
        raise ValueError(f"invalid planning timestamp {now!r}")
    return parsed


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _check_id(validator_id: str, target_scope: str, tier: ValidationTier) -> str:
    digest = sha256(f"{validator_id}|{target_scope}|{tier.value}".encode("utf-8")).hexdigest()[:16]
    return f"check:{digest}"


def _plan_id(
    manifest_id: str,
    target_scope: str,
    tier: ValidationTier,
    checks: list[ValidationCheck],
    decision: PlannerDecision,
    check_statuses: list[ValidationCheckStatus],
    performance_budget: dict[str, Any],
) -> str:
    payload = {
        "blocked_reasons": list(decision.blocked_reasons),
        "check_statuses": [
            {
                "status": status.status,
                "validator_id": status.validator_id,
            }
            for status in check_statuses
        ],
        "checks": [
            {
                "check_id": check.check_id,
                "cache_decision": check.cache_decision,
                "execution_mode": check.execution_mode,
                "execution_policy": check.execution_policy,
                "receipt_id": check.receipt_id,
            }
            for check in checks
        ],
        "manifest_id": manifest_id,
        "performance_budget": performance_budget,
        "target_scope": target_scope,
        "tier": tier.value,
    }
    digest = sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    return f"validation-plan:{digest}"
