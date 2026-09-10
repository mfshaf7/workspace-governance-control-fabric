"""Pure, non-mutating policy for Prototype maturity readiness."""

from __future__ import annotations

import re
from typing import Any

from .prototype_maturity_contracts import (
    PrototypeMaturityContracts,
    PrototypeMaturitySnapshot,
    digest,
)


CHECK_IDS = (
    "request-integrity",
    "lifecycle-source-state",
    "source-version-freshness",
    "packet-integrity",
    "required-evidence",
    "boundary-coherence",
    "security-trigger-disposition",
    "open-issue-disposition",
)
EDITABLE_KINDS = {
    "candidate-promotion": {
        "prototype-objective": "text",
        "target-user": "text",
        "expected-proof": "text",
        "accepted-scope": "list",
        "excluded-scope": "list",
        "boundary-clarifications": "text",
        "open-issue-disposition": "text",
    },
    "baseline-promotion": {
        "baseline-title": "text",
        "baseline-statement": "text",
        "accepted-summary": "text",
        "excluded-summary": "text",
        "selected-evidence-refs": "refs",
        "missing-evidence-disposition": "text",
        "issue-and-risk-disposition": "text",
    },
}
SAFE_REF = re.compile(
    r"^(?:record|repo|openproject|evidence|proof|security-review|console|wgcf)://"
    r"[A-Za-z0-9][A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]*$"
)
SAFE_HTTPS_REF = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[^\s]*)?$"
)
SECRET_MARKERS = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
)


def evaluate_prototype_maturity(
    envelope: dict[str, Any],
    snapshot: PrototypeMaturitySnapshot,
    contracts: PrototypeMaturityContracts,
) -> dict[str, Any]:
    request = envelope["request"]
    packet = envelope["packet"]
    transition = request["transition"]
    profile = contracts.policy["transition_profiles"][transition]
    lifecycle = contracts.policy["prototype_lifecycle"]["transitions"][transition]
    checks = {
        check_id: {
            "id": check_id,
            "state": "ready",
            "evidence_refs": [f"wgcf://prototype-maturity/checks/{check_id}"],
        }
        for check_id in CHECK_IDS
    }
    findings: list[dict[str, str]] = []

    def fail(
        check_id: str,
        code: str,
        detail: str,
        required_fix: str,
        *,
        owner_ref: str,
        state: str = "blocked",
    ) -> None:
        checks[check_id]["state"] = state
        findings.append({
            "code": code,
            "severity": "blocking",
            "detail": detail,
            "owner_ref": owner_ref,
            "required_fix": required_fix,
        })

    request_ref = {"id": request["request_id"], "digest": request["request_digest"]}
    packet_ref = {"id": packet["packet_id"], "digest": packet["packet_digest"]}
    if (
        packet["request_ref"] != request_ref
        or packet["prototype_id"] != request["prototype_id"]
        or packet["transition"] != transition
        or request["source_lifecycle"] != lifecycle["source"]
        or request["target_lifecycle"] != lifecycle["target"]
        or request["expected_state"]["lifecycle"] != lifecycle["source"]
    ):
        fail(
            "request-integrity",
            "maturity-artifact-binding-invalid",
            "The request, packet, Prototype identity, transition, or lifecycle pair disagree.",
            "Rebuild the maturity artifacts from one current transition request.",
            owner_ref="operator-orchestration-service",
        )
    checks["request-integrity"]["evidence_refs"] = [
        f"wgcf://prototype-maturity/requests/{request['request_id']}@{request['request_digest']}",
        f"wgcf://prototype-maturity/packets/{packet['packet_id']}@{packet['packet_digest']}",
    ]

    lifecycle_evidence = [
        f"repo://workspace-prototype-studio/prototypes.yaml@{snapshot.revision}",
        f"wgcf://prototype-maturity/record-digest/{snapshot.record_digest}",
    ]
    if not snapshot.landing_record_valid:
        fail(
            "lifecycle-source-state",
            "prototype-landing-record-invalid",
            "Prototype maturity requires a valid committed Landing record.",
            "Repair the Prototype registry and Landing record linkage.",
            owner_ref="workspace-prototype-studio",
        )
    if snapshot.lifecycle != lifecycle["source"]:
        fail(
            "lifecycle-source-state",
            "prototype-lifecycle-transition-invalid",
            f"{transition} requires committed lifecycle {lifecycle['source']}.",
            "Refresh the workflow from current Prototype Studio lifecycle truth.",
            owner_ref="workspace-prototype-studio",
        )
    if transition == "baseline-promotion" and not snapshot.candidate_record_valid:
        fail(
            "lifecycle-source-state",
            "candidate-record-invalid",
            "Baseline Promotion requires a valid committed Candidate Promotion record.",
            "Repair or complete Candidate Promotion before evaluating the baseline.",
            owner_ref="workspace-prototype-studio",
        )
    checks["lifecycle-source-state"]["evidence_refs"] = lifecycle_evidence

    observed_state = {
        "source_revision": snapshot.revision,
        "record_digest": snapshot.record_digest,
        "lifecycle": snapshot.lifecycle,
    }
    if (
        envelope["authority_revision"] != snapshot.revision
        or request["expected_state"] != observed_state
        or envelope["policy_ref"] != contracts.policy_ref
        or envelope["security_review_ref"] != contracts.security_review_ref
    ):
        fail(
            "source-version-freshness",
            "maturity-authority-stale",
            "Source, policy, or Security-review authority differs from the evaluation input.",
            "Refresh the request and packet against current pinned authority truth.",
            owner_ref="operator-orchestration-service",
            state="stale",
        )
    checks["source-version-freshness"]["evidence_refs"] = [
        f"repo://workspace-prototype-studio@{snapshot.revision}",
        f"wgcf://prototype-maturity/policy/{contracts.policy_ref['digest']}",
        f"security-review://prototype-maturity/{contracts.security_review_ref['digest']}",
    ]

    sections = packet["sections"]
    section_ids = [section["id"] for section in sections]
    expected_sections = set(profile["required_sections"])
    if (
        packet["packet_kind"] != profile["packet_kind"]
        or len(section_ids) != len(set(section_ids))
        or set(section_ids) != expected_sections
    ):
        fail(
            "packet-integrity",
            "maturity-packet-shape-invalid",
            "The packet kind or section set differs from the selected transition.",
            "Reassemble the exact transition packet.",
            owner_ref="operator-orchestration-service",
        )
    checks["packet-integrity"]["evidence_refs"] = [
        f"wgcf://prototype-maturity/packet-kind/{packet['packet_kind']}",
        "wgcf://prototype-maturity/packet-sections/" + digest(sorted(section_ids)),
    ]

    resolution_by_ref = {item.ref: item for item in snapshot.evidence}
    unresolved = [item for item in snapshot.evidence if item.state != "verified"]
    incomplete_sections = [
        section["id"]
        for section in sections
        if section["state"] != "ready" or not section["evidence_refs"]
    ]
    if incomplete_sections:
        fail(
            "required-evidence",
            "maturity-evidence-incomplete",
            "Required packet sections are not ready: " + ", ".join(sorted(incomplete_sections)) + ".",
            "Resolve each required section and attach authority-backed evidence.",
            owner_ref="workspace-prototype-studio",
        )
    if unresolved:
        fail(
            "required-evidence",
            "maturity-evidence-unresolved",
            "Evidence authority could not verify: " + ", ".join(item.ref for item in unresolved) + ".",
            "Replace each unresolved reference with committed authority-backed evidence.",
            owner_ref=unresolved[0].owner_ref,
        )
    checks["required-evidence"]["evidence_refs"] = [
        item.evidence_ref for item in snapshot.evidence
    ] or ["evidence:none"]

    boundary_errors = _boundary_errors(request, resolution_by_ref)
    if boundary_errors:
        fail(
            "boundary-coherence",
            "maturity-boundary-invalid",
            boundary_errors[0],
            "Correct the bounded editable values and source references.",
            owner_ref="operator-orchestration-service",
        )
    checks["boundary-coherence"]["evidence_refs"] = [
        "wgcf://prototype-maturity/editable-values/"
        + digest(request["inputs"]["editable_values"]),
        "wgcf://prototype-maturity/source-refs/"
        + digest(request["inputs"]["source_refs"]),
    ]

    security_sections = {
        "candidate-promotion": {"boundaries-and-risks"},
        "baseline-promotion": {"boundaries", "issues-and-risk-disposition"},
    }[transition]
    security_unready = sorted(
        section["id"]
        for section in sections
        if section["id"] in security_sections and section["state"] != "ready"
    )
    if security_unready:
        fail(
            "security-trigger-disposition",
            "security-trigger-unresolved",
            "Security-sensitive packet sections are not ready: " + ", ".join(security_unready) + ".",
            "Record the required Security or governance disposition.",
            owner_ref="security-architecture",
        )
    checks["security-trigger-disposition"]["evidence_refs"] = [
        f"security-review://prototype-maturity/{contracts.security_review_ref['digest']}",
        *[
            f"wgcf://prototype-maturity/sections/{section_id}"
            for section_id in sorted(security_sections)
        ],
    ]

    issue_field = (
        "open-issue-disposition"
        if transition == "candidate-promotion"
        else "issue-and-risk-disposition"
    )
    issue_value = request["inputs"]["editable_values"].get(issue_field)
    if not isinstance(issue_value, str) or not issue_value.strip():
        fail(
            "open-issue-disposition",
            "open-issue-disposition-missing",
            "The transition has no explicit open-issue disposition.",
            "Record the current issue disposition before promotion.",
            owner_ref="operator-orchestration-service",
        )
    checks["open-issue-disposition"]["evidence_refs"] = [
        f"wgcf://prototype-maturity/request-fields/{issue_field}"
    ]

    states = {check["state"] for check in checks.values()}
    outcome = "blocked" if "blocked" in states else "stale" if "stale" in states else "ready"
    return {
        "request_ref": request_ref,
        "packet_ref": packet_ref,
        "observed_state": observed_state,
        "outcome": outcome,
        "checks": list(checks.values()),
        "findings": findings,
    }


def _boundary_errors(
    request: dict[str, Any], resolution_by_ref: dict[str, Any]
) -> list[str]:
    transition = request["transition"]
    values = request["inputs"]["editable_values"]
    expected = EDITABLE_KINDS[transition]
    if set(values) != set(expected):
        return ["Editable values do not match the selected transition fields."]
    source_refs = request["inputs"]["source_refs"]
    if not source_refs or len(source_refs) > 50 or len(set(source_refs)) != len(source_refs):
        return ["Source references must contain between 1 and 50 unique values."]
    for ref in source_refs:
        error = _reference_error(ref)
        if error:
            return [error]
        if resolution_by_ref.get(ref) is None or resolution_by_ref[ref].state != "verified":
            return [f"Source reference is not authority-backed: {ref}"]
    for name, kind in expected.items():
        value = values.get(name)
        if kind == "text":
            error = _text_error(value, name)
            if error:
                return [error]
            continue
        if not isinstance(value, list) or not value or len(value) > 50:
            return [f"{name} must contain between 1 and 50 values."]
        if any(not isinstance(item, str) for item in value) or len(value) != len(set(value)):
            return [f"{name} must contain unique text values."]
        for item in value:
            error = _reference_error(item) if kind == "refs" else _text_error(item, name)
            if error:
                return [error]
            if kind == "refs" and (
                resolution_by_ref.get(item) is None
                or resolution_by_ref[item].state != "verified"
            ):
                return [f"Selected evidence is not authority-backed: {item}"]
    return []


def _text_error(value: Any, label: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return f"{label} must be non-empty text."
    if len(value) > 2000:
        return f"{label} exceeds 2000 characters."
    if "\x00" in value or any(marker in value for marker in SECRET_MARKERS):
        return f"{label} contains prohibited content."
    return None


def _reference_error(value: Any) -> str | None:
    error = _text_error(value, "reference")
    if error:
        return error
    assert isinstance(value, str)
    if len(value) > 512:
        return "Reference exceeds 512 characters."
    if not (SAFE_REF.fullmatch(value) or SAFE_HTTPS_REF.fullmatch(value)):
        return f"Unsupported reference form: {value}"
    if "\\" in value or any(part == ".." for part in value.split("/")):
        return f"Unsafe reference path: {value}"
    return None
