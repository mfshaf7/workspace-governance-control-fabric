"""Pinned Delivery ART contract loading and canonical projections.

Workspace Governance remains the schema authority. This module verifies the
repo-local runtime snapshot against its digest manifest before using it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from .canonical_json import canonical_digest, canonicalization_errors


DEFAULT_CONTRACT_ROOT = (
    Path(__file__).resolve().parents[4] / "contracts" / "delivery-art"
)
DELIVERY_ART_CONTRACT_ROOT_ENV = "WGCF_DELIVERY_ART_CONTRACT_ROOT"


class DeliveryArtContractError(ValueError):
    """The pinned authority bundle or an artifact violates its contract."""


def _objects(value: Any) -> list[dict[str, Any]]:
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _strings(value: Any) -> list[str]:
    return [entry for entry in value if isinstance(entry, str)] if isinstance(value, list) else []


def _graph_is_acyclic(nodes: set[str], edges: list[tuple[str, str]]) -> bool:
    adjacency = {node: [] for node in nodes}
    indegree = {node: 0 for node in nodes}
    for source, target in edges:
        if source not in nodes or target not in nodes:
            continue
        adjacency[source].append(target)
        indegree[target] += 1
    ready = [node for node, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for target in adjacency[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    return visited == len(nodes)


def _architecture_scope_fingerprint(artifact: dict[str, Any]) -> str:
    decision = artifact.get("decision") if isinstance(artifact.get("decision"), dict) else {}
    return canonical_digest(
        {
            "schema_version": artifact.get("schema_version"),
            "artifact_type": artifact.get("artifact_type"),
            "delivery_id": artifact.get("delivery_id"),
            "covered_work_item_ids": artifact.get("covered_work_item_ids"),
            "source_snapshot": artifact.get("source_snapshot"),
            "architecture": artifact.get("architecture"),
            "conformance_plan": artifact.get("conformance_plan"),
            "decision_status": decision.get("status"),
        },
    )


def _architecture_semantic_errors(artifact: dict[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    architecture = artifact.get("architecture")
    architecture = architecture if isinstance(architecture, dict) else {}
    covered = set(_strings(artifact.get("covered_work_item_ids")))
    owner_map = _objects(architecture.get("descendant_owner_map"))
    owner_ids = [entry.get("work_item_id") for entry in owner_map]
    owner_by_work_item = {
        entry["work_item_id"]: entry["owner_repo"]
        for entry in owner_map
        if isinstance(entry.get("work_item_id"), str)
        and isinstance(entry.get("owner_repo"), str)
    }

    if len(owner_ids) != len(set(owner_ids)):
        errors.append("architecture.descendant_owner_map must contain one entry per work item")
    if set(owner_ids) != covered:
        errors.append("architecture.descendant_owner_map must exactly cover covered_work_item_ids")

    parent_by_item = {
        entry["work_item_id"]: entry.get("parent_work_item_id")
        for entry in owner_map
        if isinstance(entry.get("work_item_id"), str)
    }
    if parent_by_item and not any(parent is None for parent in parent_by_item.values()):
        errors.append("architecture.descendant_owner_map must contain at least one root")
    for work_item_id, parent in parent_by_item.items():
        if isinstance(parent, str) and parent not in covered:
            errors.append(
                f"architecture descendant {work_item_id} references unknown parent {parent}",
            )
    for start in parent_by_item:
        seen: set[str] = set()
        current: str | None = start
        while isinstance(current, str) and current in parent_by_item:
            if current in seen:
                errors.append("architecture.descendant_owner_map parent links must be acyclic")
                current = None
                break
            seen.add(current)
            current = parent_by_item[current]

    schema_version = artifact.get("schema_version")
    if schema_version == 1:
        graph = architecture.get("dependency_merge_dag")
        graph = graph if isinstance(graph, dict) else {}
        nodes = set(_strings(graph.get("nodes")))
        if nodes != covered:
            errors.append(
                "architecture.dependency_merge_dag.nodes must exactly cover covered_work_item_ids",
            )
        precedence: list[tuple[str, str]] = []
        for edge in _objects(graph.get("edges")):
            source, target = edge.get("from"), edge.get("to")
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            if source not in nodes or target not in nodes:
                errors.append("architecture dependency edge references unknown nodes")
                continue
            precedence.append((target, source) if edge.get("relation") == "depends_on" else (source, target))
        if not _graph_is_acyclic(nodes, precedence):
            errors.append("architecture.dependency_merge_dag must be acyclic")
        merge_order = _strings(graph.get("merge_order"))
        owner_repos = set(owner_by_work_item.values())
        if len(merge_order) != len(set(merge_order)) or set(merge_order) != owner_repos:
            errors.append(
                "architecture.dependency_merge_dag.merge_order must exactly cover descendant owner repos",
            )
        else:
            positions = {repo: index for index, repo in enumerate(merge_order)}
            for before, after in precedence:
                before_repo = owner_by_work_item.get(before)
                after_repo = owner_by_work_item.get(after)
                if (
                    before_repo is not None
                    and after_repo is not None
                    and before_repo != after_repo
                    and positions[before_repo] >= positions[after_repo]
                ):
                    errors.append(
                        f"architecture merge order violates {before} before {after}",
                    )

    if schema_version in {2, 3}:
        execution_plan_by_work_item: dict[str, dict[str, Any]] = {}
        emitted_gate_authorities: dict[str, list[str]] = {}
        if schema_version == 2:
            work_graph = architecture.get("work_dependency_graph")
            work_graph = work_graph if isinstance(work_graph, dict) else {}
            work_nodes = set(_strings(work_graph.get("nodes")))
            if work_nodes != covered:
                errors.append(
                    "architecture.work_dependency_graph.nodes must exactly cover covered_work_item_ids",
                )
            work_edges: list[tuple[str, str]] = []
            for edge in _objects(work_graph.get("edges")):
                prerequisite = edge.get("prerequisite_work_item_id")
                dependent = edge.get("dependent_work_item_id")
                if not isinstance(prerequisite, str) or not isinstance(dependent, str):
                    continue
                if prerequisite not in work_nodes or dependent not in work_nodes:
                    errors.append("architecture work dependency edge references unknown nodes")
                    continue
                work_edges.append((prerequisite, dependent))
            if not _graph_is_acyclic(work_nodes, work_edges):
                errors.append("architecture.work_dependency_graph must be acyclic")
        else:
            execution_plan = _objects(architecture.get("work_item_execution_plan"))
            execution_plan_ids = [
                entry.get("work_item_id")
                for entry in execution_plan
                if isinstance(entry.get("work_item_id"), str)
            ]
            if len(execution_plan_ids) != len(set(execution_plan_ids)):
                errors.append(
                    "architecture.work_item_execution_plan must contain one entry per work item",
                )
            if set(execution_plan_ids) != covered:
                errors.append(
                    "architecture.work_item_execution_plan must exactly cover covered_work_item_ids",
                )

            start_edges: list[tuple[str, str]] = []
            combined_schedule_edges: list[tuple[str, str]] = []
            for entry in execution_plan:
                work_item_id = entry.get("work_item_id")
                if not isinstance(work_item_id, str):
                    continue
                execution_plan_by_work_item[work_item_id] = entry
                start_prerequisites = set(_strings(entry.get("start_after_work_item_ids")))
                close_prerequisites = set(_strings(entry.get("close_after_work_item_ids")))
                repeated_prerequisites = start_prerequisites & close_prerequisites
                if repeated_prerequisites:
                    errors.append(
                        f"architecture execution plan {work_item_id} repeats "
                        "prerequisites across start_after and close_after: "
                        + ", ".join(sorted(repeated_prerequisites)),
                    )
                all_prerequisites = start_prerequisites | close_prerequisites
                unknown_prerequisites = all_prerequisites - covered
                if unknown_prerequisites:
                    errors.append(
                        f"architecture execution plan {work_item_id} references "
                        "unknown prerequisite work items: "
                        + ", ".join(sorted(unknown_prerequisites)),
                    )
                if work_item_id in all_prerequisites:
                    errors.append(
                        f"architecture execution plan {work_item_id} cannot depend on itself",
                    )
                valid_start_prerequisites = (
                    start_prerequisites - unknown_prerequisites - {work_item_id}
                )
                valid_close_prerequisites = (
                    close_prerequisites - unknown_prerequisites - {work_item_id}
                )
                start_edges.extend(
                    (prerequisite, work_item_id)
                    for prerequisite in valid_start_prerequisites
                )
                combined_schedule_edges.extend(
                    (prerequisite, work_item_id)
                    for prerequisite in valid_start_prerequisites | valid_close_prerequisites
                )
                for gate_id in _strings(entry.get("emits_human_gate_ids")):
                    emitted_gate_authorities.setdefault(gate_id, []).append(work_item_id)
            if not _graph_is_acyclic(covered, start_edges):
                errors.append(
                    "architecture.work_item_execution_plan start prerequisites must be acyclic",
                )
            if not _graph_is_acyclic(covered, combined_schedule_edges):
                errors.append(
                    "architecture.work_item_execution_plan has no executable start-and-close schedule",
                )

        landing_units = _objects(architecture.get("landing_units"))
        landing_unit_ids = [unit.get("id") for unit in landing_units]
        landing_unit_id_set = set(landing_unit_ids)
        if len(landing_unit_ids) != len(landing_unit_id_set):
            errors.append("architecture.landing_units ids must be unique")
        assigned: list[str] = []
        source_backed_ids: set[str] = set()
        for unit in landing_units:
            unit_id = unit.get("id")
            if unit.get("source_backed") is True and isinstance(unit_id, str):
                source_backed_ids.add(unit_id)
            for work_item_id in _strings(unit.get("covered_work_item_ids")):
                assigned.append(work_item_id)
                if work_item_id not in covered:
                    errors.append(
                        f"architecture Landing Unit {unit_id} references unknown work item {work_item_id}",
                    )
                expected_owner = owner_by_work_item.get(work_item_id)
                if expected_owner is not None and unit.get("owner_repo") != expected_owner:
                    errors.append(
                        f"architecture Landing Unit {unit_id} owner does not match {work_item_id} owner",
                    )
        if set(assigned) != covered:
            errors.append("architecture.landing_units must exactly cover covered_work_item_ids")
        if len(assigned) != len(set(assigned)):
            errors.append("architecture.landing_units must assign every work item exactly once")

        source_graph = architecture.get("source_landing_graph")
        source_graph = source_graph if isinstance(source_graph, dict) else {}
        source_nodes = set(_strings(source_graph.get("nodes")))
        if source_nodes != source_backed_ids:
            errors.append(
                "architecture.source_landing_graph.nodes must exactly cover source-backed Landing Units",
            )
        source_edges: list[tuple[str, str]] = []
        for edge in _objects(source_graph.get("edges")):
            prerequisite = edge.get("prerequisite_landing_unit_id")
            dependent = edge.get("dependent_landing_unit_id")
            if not isinstance(prerequisite, str) or not isinstance(dependent, str):
                continue
            if prerequisite not in source_nodes or dependent not in source_nodes:
                errors.append("architecture source landing edge references unknown nodes")
                continue
            source_edges.append((prerequisite, dependent))
        if not _graph_is_acyclic(source_nodes, source_edges):
            errors.append("architecture.source_landing_graph must be acyclic")

        gates = _objects(architecture.get("required_human_gates"))
        gate_ids = [gate.get("gate_id") for gate in gates]
        if len(gate_ids) != len(set(gate_ids)):
            errors.append("architecture.required_human_gates ids must be unique")
        for gate in gates:
            gate_id = gate.get("gate_id")
            authority_work_item_id = gate.get("authority_work_item_id")
            if authority_work_item_id not in covered:
                errors.append(
                    f"architecture human gate {gate_id} references unknown authority work item",
                )
            expected_owner = owner_by_work_item.get(authority_work_item_id)
            if expected_owner is not None and gate.get("authority_owner_repo") != expected_owner:
                errors.append(
                    f"architecture human gate {gate_id} authority owner does not match its work item",
                )
            affected = set(_strings(gate.get("affected_landing_unit_ids")))
            if affected - landing_unit_id_set:
                errors.append(
                    f"architecture human gate {gate_id} references unknown Landing Units",
                )
            if gate.get("blocked_transition") == "before_source_merge" and affected - source_backed_ids:
                errors.append(
                    f"architecture human gate {gate_id} blocks source merge for non-source Landing Units",
                )
            if schema_version == 3:
                evidence_prerequisites = set(
                    _strings(gate.get("evidence_prerequisite_work_item_ids")),
                )
                unknown_evidence_prerequisites = evidence_prerequisites - covered
                if unknown_evidence_prerequisites:
                    errors.append(
                        f"architecture human gate {gate_id} references unknown "
                        "evidence prerequisite work items: "
                        + ", ".join(sorted(unknown_evidence_prerequisites)),
                    )
                authority_plan = execution_plan_by_work_item.get(
                    authority_work_item_id,
                    {},
                )
                authority_prerequisites = set(
                    _strings(authority_plan.get("start_after_work_item_ids")),
                ) | set(_strings(authority_plan.get("close_after_work_item_ids")))
                missing_authority_prerequisites = (
                    evidence_prerequisites - authority_prerequisites
                )
                if missing_authority_prerequisites:
                    errors.append(
                        f"architecture human gate {gate_id} evidence prerequisites "
                        "are absent from authority work item "
                        f"{authority_work_item_id} execution prerequisites: "
                        + ", ".join(sorted(missing_authority_prerequisites)),
                    )

        if schema_version == 3:
            declared_gate_ids = set(gate_ids)
            emitted_gate_ids = set(emitted_gate_authorities)
            unknown_emitted_gates = emitted_gate_ids - declared_gate_ids
            if unknown_emitted_gates:
                errors.append(
                    "architecture.work_item_execution_plan emits unknown human gates: "
                    + ", ".join(sorted(unknown_emitted_gates)),
                )
            missing_emitted_gates = declared_gate_ids - emitted_gate_ids
            if missing_emitted_gates:
                errors.append(
                    "architecture.work_item_execution_plan must emit every declared human gate: "
                    + ", ".join(sorted(missing_emitted_gates)),
                )
            gate_by_id = {
                gate.get("gate_id"): gate
                for gate in gates
                if isinstance(gate.get("gate_id"), str)
            }
            for gate_id, emitter_ids in emitted_gate_authorities.items():
                if len(emitter_ids) != 1:
                    errors.append(
                        f"architecture human gate {gate_id} must be emitted exactly once",
                    )
                    continue
                gate = gate_by_id.get(gate_id)
                if (
                    gate is not None
                    and gate.get("authority_work_item_id") != emitter_ids[0]
                ):
                    errors.append(
                        f"architecture human gate {gate_id} must be emitted by its "
                        f"authority work item {gate.get('authority_work_item_id')}",
                    )
            for work_item_id, owner_repo in owner_by_work_item.items():
                if owner_repo != "security-architecture":
                    continue
                if not _strings(
                    execution_plan_by_work_item.get(work_item_id, {}).get(
                        "emits_human_gate_ids",
                    ),
                ):
                    errors.append(
                        f"architecture Security-owned work item {work_item_id} "
                        "must emit at least one explicit human gate",
                    )

    source_snapshot = artifact.get("source_snapshot")
    source_snapshot = source_snapshot if isinstance(source_snapshot, dict) else {}
    revision_repos = [
        entry.get("repo") for entry in _objects(source_snapshot.get("repo_revisions"))
    ]
    owner_repos = set(owner_by_work_item.values())
    if len(revision_repos) != len(set(revision_repos)):
        errors.append("source_snapshot.repo_revisions must contain one entry per owner repo")
    if set(revision_repos) != owner_repos:
        errors.append(
            "source_snapshot.repo_revisions must exactly cover descendant owner repos",
        )
    if artifact.get("scope_fingerprint") != _architecture_scope_fingerprint(artifact):
        errors.append("scope_fingerprint does not match the architecture scope projection")
    return tuple(dict.fromkeys(errors))


@dataclass(frozen=True)
class DeliveryArtContractBundle:
    """Digest-verified schema snapshot consumed by the WGCF runtime."""

    root: Path
    source_commit: str
    validators: dict[str, Draft202012Validator]

    @classmethod
    def load(
        cls,
        root: str | Path | None = None,
    ) -> DeliveryArtContractBundle:
        configured_root = root or os.environ.get(DELIVERY_ART_CONTRACT_ROOT_ENV) or DEFAULT_CONTRACT_ROOT
        resolved_root = Path(configured_root).resolve()
        manifest_path = resolved_root / "manifest.json"
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes)
        except (OSError, json.JSONDecodeError) as exc:
            raise DeliveryArtContractError(
                "Delivery ART authority manifest is unavailable or invalid",
            ) from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise DeliveryArtContractError("unsupported Delivery ART authority manifest")
        source = manifest.get("source")
        if (
            not isinstance(source, dict)
            or source.get("repo") != "workspace-governance"
            or not isinstance(source.get("commit"), str)
            or len(source["commit"]) != 40
        ):
            raise DeliveryArtContractError("Delivery ART authority source is not pinned")
        schema_entries = manifest.get("schemas")
        if not isinstance(schema_entries, dict) or not schema_entries:
            raise DeliveryArtContractError("Delivery ART authority manifest has no schemas")

        format_checker = FormatChecker()
        validators: dict[str, Draft202012Validator] = {}
        for artifact_type, entry in schema_entries.items():
            if not isinstance(artifact_type, str) or not isinstance(entry, dict):
                raise DeliveryArtContractError("Delivery ART schema manifest is malformed")
            schema_path = resolved_root / str(entry.get("path", ""))
            try:
                schema_bytes = schema_path.read_bytes()
            except OSError as exc:
                raise DeliveryArtContractError(
                    f"Delivery ART schema snapshot is unavailable: {schema_path.name}",
                ) from exc
            expected_digest = entry.get("sha256")
            actual_digest = hashlib.sha256(schema_bytes).hexdigest()
            if expected_digest != actual_digest:
                raise DeliveryArtContractError(
                    f"Delivery ART schema snapshot does not match manifest: {schema_path.name}",
                )
            try:
                schema = json.loads(schema_bytes)
                Draft202012Validator.check_schema(schema)
            except (json.JSONDecodeError, SchemaError) as exc:
                raise DeliveryArtContractError(
                    f"Delivery ART schema snapshot is invalid: {schema_path.name}",
                ) from exc
            validators[artifact_type] = Draft202012Validator(
                schema,
                format_checker=format_checker,
            )
        return cls(
            root=resolved_root,
            source_commit=source["commit"],
            validators=validators,
        )

    def validation_errors(self, artifact: Any) -> tuple[str, ...]:
        canonical_errors = canonicalization_errors(artifact)
        if canonical_errors:
            return tuple(canonical_errors)
        if not isinstance(artifact, dict):
            return ("artifact must be an object",)
        validator = self.validators.get(str(artifact.get("artifact_type", "")))
        if validator is None:
            return (f"unsupported artifact_type {artifact.get('artifact_type')!r}",)
        errors = sorted(
            validator.iter_errors(artifact),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        schema_errors = tuple(
            f"{_json_path(error.absolute_path)}: {error.message}"
            for error in errors
        )
        if schema_errors:
            return schema_errors
        if artifact.get("artifact_type") == "delivery_art_architecture_packet":
            return _architecture_semantic_errors(artifact)
        return ()

    def require_valid(self, artifact: Any) -> None:
        errors = self.validation_errors(artifact)
        if errors:
            raise DeliveryArtContractError(errors[0])


def review_packet_readiness_subject_digest(packet: dict[str, Any]) -> str:
    """Return the cycle-safe operating-readiness subject digest."""

    projection = copy.deepcopy(packet)
    projection.pop("custody", None)
    projection.pop("finalized_at", None)
    projection.pop("integrity", None)
    readiness = projection.get("readiness")
    if isinstance(readiness, dict):
        readiness.pop("evaluated_at", None)
        readiness.pop("receipt_refs", None)
        readiness.pop("subject_digest", None)
    return canonical_digest(projection)


def operating_readiness_subject(candidate: dict[str, Any]) -> dict[str, Any]:
    """Build the timestamp-free semantic subject evaluated before finalization."""

    subject = copy.deepcopy(candidate)
    subject["status"] = "finalized"
    subject["finalized_at"] = None
    subject["readiness"] = {
        "evaluated_at": None,
        "level": "operating-ready",
        "receipt_refs": [],
        "subject_digest": None,
    }
    subject["readiness"]["subject_digest"] = review_packet_readiness_subject_digest(
        subject,
    )
    return subject


def _json_path(parts: Any) -> str:
    rendered = "$"
    for part in parts:
        rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
    return rendered
