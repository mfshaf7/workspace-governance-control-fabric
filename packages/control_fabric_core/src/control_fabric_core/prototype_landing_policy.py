"""Pure, non-mutating policy for Prototype Landing readiness."""

from __future__ import annotations

from typing import Any

from .prototype_landing_contracts import PrototypeLandingSnapshot, digest


CHECK_IDS = (
    "entry-integrity",
    "identity-availability",
    "required-metadata",
    "support-profile-integrity",
    "support-row-readiness",
    "source-custody-coherence",
    "source-version-freshness",
    "data-and-mutation-boundary",
    "visibility-and-exposure",
    "security-trigger-disposition",
    "expected-mutation-set",
)
SUPPORT_DIMENSIONS = {
    "source", "studio-home", "interface", "runtime", "data",
    "integration", "tooling", "evidence", "visibility", "recovery",
}
REFERENCE_ONLY = {
    "reference-dedicated-owner-source",
    "reference-shared-owner-source",
}


def evaluate_prototype_landing(
    envelope: dict[str, Any], snapshot: PrototypeLandingSnapshot
) -> dict[str, Any]:
    entry = envelope["entry_packet"]
    request = envelope["request"]
    plan = envelope["plan"]
    checks = {
        check_id: {
            "id": check_id,
            "state": "ready",
            "evidence_refs": [f"contract:prototype-landing#{check_id}"],
        }
        for check_id in CHECK_IDS
    }
    findings: list[dict[str, str]] = []

    def fail(
        check_id: str,
        code: str,
        message: str,
        next_action: str,
        *,
        owner: str,
        state: str = "blocked",
    ) -> None:
        checks[check_id]["state"] = state
        findings.append({
            "code": code,
            "severity": "blocking",
            "message": message,
            "owner_ref": owner,
            "next_action": next_action,
        })

    entry_ref = {"id": entry["entry_id"], "digest": entry["packet_digest"]}
    request_ref = {"id": request["request_id"], "digest": request["request_digest"]}
    if request["entry_packet_ref"] != entry_ref or plan["request_ref"] != request_ref:
        fail(
            "entry-integrity", "entry-binding-invalid",
            "The Landing request and plan do not preserve the exact artifact chain.",
            "Rebuild the request and plan from the immutable Entry Packet.",
            owner="operator-orchestration-service",
        )
    checks["entry-integrity"]["evidence_refs"] = [
        f"{entry['entry_id']}@{entry['packet_digest']}"
    ]

    if snapshot.record_present:
        fail(
            "identity-availability", "prototype-identity-unavailable",
            "The reserved Prototype identity already exists in committed Studio truth.",
            "Choose a new stable Prototype identity or use the existing record workflow.",
            owner="workspace-prototype-studio",
        )
    checks["identity-availability"]["evidence_refs"] = [
        f"repo://workspace-prototype-studio/prototypes.yaml@{snapshot.revision}"
    ]

    if request["starting_lifecycle"] != "exploring" or not request["operator_accepted"]:
        fail(
            "required-metadata", "landing-metadata-unaccepted",
            "Prototype metadata must be explicitly accepted for the exploring lifecycle.",
            "Correct and accept the Landing metadata.",
            owner="operator-orchestration-service",
        )
    checks["required-metadata"]["evidence_refs"] = [
        f"{request['request_id']}@{request['request_digest']}"
    ]

    rows = request["setup"]["support_rows"]
    dimensions = [row["dimension"] for row in rows]
    generated = request["setup"]["support_profile"] != "custom"
    if set(dimensions) != SUPPORT_DIMENSIONS or len(set(dimensions)) != len(rows) or any(
        row["generated"] != generated for row in rows
    ):
        fail(
            "support-profile-integrity", "support-profile-invalid",
            "Support rows do not match the selected profile and complete dimension set.",
            "Regenerate or correct the support profile configuration.",
            owner="operator-orchestration-service",
        )
    checks["support-profile-integrity"]["evidence_refs"] = [
        f"support-profile:{request['setup']['support_profile']}"
    ]

    unresolved = sorted(
        row["dimension"] for row in rows if row["state"] in {"unknown", "blocked"}
    )
    if unresolved:
        fail(
            "support-row-readiness", "support-rows-unresolved",
            "Required support remains unresolved: " + ", ".join(unresolved) + ".",
            "Resolve each unknown or blocked support row.",
            owner="workspace-prototype-studio",
        )
    checks["support-row-readiness"]["evidence_refs"] = [
        "support-rows:" + digest(rows)
    ]

    source = request["source_plan"]
    posture = source["posture"]
    slug = request["prototype"]["id"].removeprefix("prototype:")
    expected_ref = f"repo://workspace-prototype-studio/prototypes/{slug}"
    if plan["prototype_id"] != request["prototype"]["id"] or plan["source_plan"] != source:
        fail(
            "source-custody-coherence", "source-plan-binding-invalid",
            "The Landing plan does not preserve the request identity and source custody.",
            "Rebuild the plan from the accepted request.",
            owner="operator-orchestration-service",
        )
    elif posture == "create-studio-source":
        if (
            source["source_ref"] != expected_ref
            or source["source_revision"] is not None
            or source["origin_digest"] is not None
            or source["imported_content_digest"] is not None
            or snapshot.source_exists
        ):
            fail(
                "source-custody-coherence", "create-source-posture-invalid",
                "Create-source custody must target an absent canonical Studio source path.",
                "Correct the source posture or use the existing-source workflow.",
                owner="workspace-prototype-studio",
            )
    elif posture == "use-existing-studio-source":
        if (
            source["source_ref"] != expected_ref
            or source["source_revision"] != snapshot.revision
            or source["origin_digest"] is not None
            or source["imported_content_digest"] is not None
            or not snapshot.source_exists
        ):
            fail(
                "source-custody-coherence", "existing-source-posture-invalid",
                "Existing Studio custody must bind the present canonical path and current revision.",
                "Refresh the source plan from committed Prototype Studio truth.",
                owner="workspace-prototype-studio",
            )
    elif posture in REFERENCE_ONLY:
        fail(
            "source-custody-coherence", "external-source-authority-unavailable",
            "Referenced source ownership and revision cannot yet be resolved authoritatively.",
            "Provide an admitted source-owner resolver before retrying readiness.",
            owner="workspace-governance-control-fabric",
        )
    else:
        fail(
            "source-custody-coherence", "import-evidence-unavailable",
            "Imported content has no admitted bounded inspection receipt.",
            "Produce an authoritative import-safety receipt before retrying readiness.",
            owner="workspace-governance-control-fabric",
        )
    checks["source-custody-coherence"]["evidence_refs"] = [
        f"source-posture:{posture}", f"source-ref:{source['source_ref']}"
    ]

    observed = {
        "registry_digest": snapshot.registry_digest,
        "record_present": snapshot.record_present,
        "source_revision": snapshot.revision,
    }
    expected = request["expected_state"]
    expected_projection = {
        "registry_digest": expected["registry_digest"],
        "record_present": expected["record_present"],
        "source_revision": expected["source_revision"],
    }
    if (
        envelope["authority_revision"] != snapshot.revision
        or expected["record_digest"] is not None
        or expected_projection != observed
    ):
        fail(
            "source-version-freshness", "source-state-stale",
            "The evaluation no longer matches committed Prototype Studio source truth.",
            "Refresh the request and plan against the current Studio revision.",
            owner="workspace-prototype-studio", state="stale",
        )
    checks["source-version-freshness"]["evidence_refs"] = [
        f"git://workspace-prototype-studio@{snapshot.revision}",
        f"registry:{snapshot.registry_digest}",
    ]

    setup = request["setup"]
    if setup["data_mode"] not in {"mock", "synthetic"} or setup["mutation_boundary"] == "real-system-blocked":
        fail(
            "data-and-mutation-boundary", "data-or-mutation-boundary-denied",
            "Prototype Landing is limited to mock or synthetic data and non-real mutation boundaries.",
            "Select a bounded data and mutation posture.",
            owner="security-architecture",
        )
    checks["data-and-mutation-boundary"]["evidence_refs"] = [
        f"data-mode:{setup['data_mode']}", f"mutation-boundary:{setup['mutation_boundary']}"
    ]

    if setup["visibility"] not in {"private", "operator-review"}:
        fail(
            "visibility-and-exposure", "external-exposure-denied",
            "Client or public exposure is outside the approved Landing boundary.",
            "Use private or operator-review visibility for Landing.",
            owner="security-architecture",
        )
    checks["visibility-and-exposure"]["evidence_refs"] = [
        f"visibility:{setup['visibility']}"
    ]

    security_triggers = _security_triggers(entry, request)
    if security_triggers:
        fail(
            "security-trigger-disposition", "security-triggers-unresolved",
            "Security-sensitive Landing inputs require authoritative disposition.",
            "Resolve the listed triggers through Security Architecture.",
            owner="security-architecture",
        )
    checks["security-trigger-disposition"]["evidence_refs"] = (
        security_triggers or ["security-trigger:none"]
    )

    mutation_error = _mutation_plan_error(request, plan)
    if mutation_error:
        fail(
            "expected-mutation-set", "expected-mutation-set-invalid",
            mutation_error,
            "Rebuild the Landing plan with the exact bounded output set.",
            owner="workspace-prototype-studio",
        )
    checks["expected-mutation-set"]["evidence_refs"] = [
        f"{plan['plan_id']}@{plan['plan_digest']}"
    ]

    states = {check["state"] for check in checks.values()}
    outcome = "blocked" if "blocked" in states else "stale" if "stale" in states else "ready"
    return {
        "observed_state": observed,
        "outcome": outcome,
        "checks": list(checks.values()),
        "findings": findings,
        "security_trigger_refs": security_triggers,
        "request_ref": request_ref,
    }


def _security_triggers(entry: dict[str, Any], request: dict[str, Any]) -> list[str]:
    setup = request["setup"]
    security_terms = ("credential", "secret", "security", "exposure", "real-data", "identity")
    triggers = [
        f"entry-constraint:{item['code']}"
        for item in entry["constraints"]
        if any(term in item["code"] for term in security_terms)
    ]
    if setup["data_mode"].startswith("real-"):
        triggers.append(f"data-mode:{setup['data_mode']}")
    if setup["visibility"] in {"client-review", "public-demo"}:
        triggers.append(f"visibility:{setup['visibility']}")
    if request["source_plan"]["posture"] in REFERENCE_ONLY | {"import-to-studio"}:
        triggers.append(f"source-posture:{request['source_plan']['posture']}")
    return sorted(set(triggers))


def _mutation_plan_error(request: dict[str, Any], plan: dict[str, Any]) -> str | None:
    mutation_set = set(plan["mutation_set"])
    output_kinds = [item["kind"] for item in plan["expected_outputs"]]
    if mutation_set != set(output_kinds) or len(output_kinds) != len(set(output_kinds)):
        return "Expected outputs must cover each declared mutation exactly once."
    if any(not item["required"] for item in plan["expected_outputs"]):
        return "Every declared Landing output must be required."
    if not {"registry-record", "prototype-docs"}.issubset(mutation_set):
        return "Landing must produce a registry record and Prototype documentation."
    posture = request["source_plan"]["posture"]
    if posture in {"create-studio-source", "import-to-studio"} and "prototype-source" not in mutation_set:
        return f"{posture} must include the Prototype source output."
    if posture in REFERENCE_ONLY and mutation_set & {"prototype-source", "fixtures"}:
        return "Reference-only Landing cannot copy source or fixtures."
    if posture == "use-existing-studio-source" and "prototype-source" in mutation_set:
        return "Existing Studio source cannot be rewritten by Landing."
    slug = request["prototype"]["id"].removeprefix("prototype:")
    targets = {
        "registry-record": "prototypes.yaml",
        "prototype-docs": f"docs/prototypes/{slug}",
        "prototype-source": f"prototypes/{slug}",
        "fixtures": f"fixtures/prototypes/{slug}",
        "preview-profile-draft": f"records/prototype-preview-profiles/{slug}.yaml",
        "validation-plan": f"records/prototype-landings/{slug}/validation-plan.yaml",
    }
    for output in plan["expected_outputs"]:
        if output["target_ref"] != targets[output["kind"]]:
            return f"{output['kind']} must target {targets[output['kind']]}."
    return None
