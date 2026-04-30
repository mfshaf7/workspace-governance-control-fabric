"""Runtime manifest-to-graph ingestion primitives.

This module converts validated governance manifests into fabric-local graph
records. It does not persist them and does not mutate upstream authorities.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any

from .manifests import validate_governance_manifest


@dataclass(frozen=True)
class ManifestGraphNode:
    """Fabric-local graph node derived from a manifest declaration."""

    node_id: str
    node_type: str
    owner_repo: str | None
    external_ref: str | None
    properties: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ManifestGraphEdge:
    """Fabric-local directed graph edge derived from a manifest declaration."""

    edge_id: str
    source_node_id: str
    target_node_id: str
    edge_type: str
    properties: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ManifestGraph:
    """In-memory graph projection produced from one governance manifest."""

    manifest_id: str
    nodes: tuple[ManifestGraphNode, ...]
    edges: tuple[ManifestGraphEdge, ...]

    def to_records(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "edges": [edge.to_record() for edge in self.edges],
            "nodes": [node.to_record() for node in self.nodes],
        }


def build_manifest_graph(manifest: dict[str, Any]) -> ManifestGraph:
    """Build deterministic graph records from a valid governance manifest."""

    validation = validate_governance_manifest(manifest)
    if not validation.valid:
        raise ValueError(f"manifest is invalid: {'; '.join(validation.errors)}")

    nodes: dict[str, ManifestGraphNode] = {}
    edges: dict[str, ManifestGraphEdge] = {}

    def add_node(node: ManifestGraphNode) -> None:
        nodes[node.node_id] = node

    def add_edge(
        source_node_id: str,
        target_node_id: str,
        edge_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        edge = ManifestGraphEdge(
            edge_id=_edge_id(source_node_id, edge_type, target_node_id),
            edge_type=edge_type,
            properties=properties or {},
            source_node_id=source_node_id,
            target_node_id=target_node_id,
        )
        edges[edge.edge_id] = edge

    authority_nodes = _authority_nodes(manifest)
    for authority_id, node in authority_nodes.items():
        add_node(node)

    repo_nodes = _repo_nodes(manifest)
    for repo_id, node in repo_nodes.items():
        add_node(node)
        _add_authority_edges(add_edge, node.node_id, manifest["repos"], "repo_id", repo_id)

    for component in manifest["components"]:
        component_id = component["component_id"].strip()
        component_node = _manifest_node(
            prefix="component",
            raw_id=component_id,
            node_type="component",
            owner_repo=component["owner_repo"],
            external_ref=",".join(component.get("source_paths") or []) or None,
            properties={
                "authority_ref_ids": component["authority_ref_ids"],
                "component_type": component["component_type"],
                "manifest_id": manifest["manifest_id"],
                "source_paths": component.get("source_paths") or [],
            },
        )
        add_node(component_node)
        _add_authority_edges(add_edge, component_node.node_id, manifest["components"], "component_id", component_id)
        owner_repo_node = repo_nodes.get(component["owner_repo"])
        if owner_repo_node:
            add_edge(component_node.node_id, owner_repo_node.node_id, "owned-by-repo")

    for validator in manifest["validators"]:
        validator_id = validator["validator_id"].strip()
        validator_node = _manifest_node(
            prefix="validator",
            raw_id=validator_id,
            node_type="validator",
            owner_repo=validator["owner_repo"],
            external_ref=validator["command"],
            properties={
                "authority_ref_ids": validator["authority_ref_ids"],
                "command": validator["command"],
                "manifest_id": manifest["manifest_id"],
                "scopes": validator["scopes"],
            },
        )
        add_node(validator_node)
        _add_authority_edges(add_edge, validator_node.node_id, manifest["validators"], "validator_id", validator_id)
        for scope in validator["scopes"]:
            target_node_id = _resolve_scope_node_id(scope, nodes)
            add_edge(
                validator_node.node_id,
                target_node_id,
                "validates-scope",
                {"scope": scope, "target_resolved": target_node_id in nodes},
            )

    for projection in manifest["projections"]:
        projection_id = projection["projection_id"].strip()
        output_ref = projection["output_ref"]
        projection_node = _manifest_node(
            prefix="projection",
            raw_id=projection_id,
            node_type="projection",
            owner_repo=projection["owner_repo"],
            external_ref=f"{output_ref['repo']}:{output_ref['path']}",
            properties={
                "manifest_id": manifest["manifest_id"],
                "output_ref": output_ref,
                "source_ref_ids": projection["source_ref_ids"],
            },
        )
        add_node(projection_node)
        for authority_id in projection["source_ref_ids"]:
            add_edge(projection_node.node_id, _node_id("authority", authority_id), "projects-from-authority")
        output_repo_node = repo_nodes.get(output_ref["repo"])
        if output_repo_node:
            add_edge(projection_node.node_id, output_repo_node.node_id, "emits-to-repo")

    return ManifestGraph(
        edges=tuple(edges[key] for key in sorted(edges)),
        manifest_id=manifest["manifest_id"],
        nodes=tuple(nodes[key] for key in sorted(nodes)),
    )


def _authority_nodes(manifest: dict[str, Any]) -> dict[str, ManifestGraphNode]:
    nodes: dict[str, ManifestGraphNode] = {}
    for authority_ref in manifest["authority_refs"]:
        authority_id = authority_ref["authority_id"].strip()
        nodes[authority_id] = _manifest_node(
            prefix="authority",
            raw_id=authority_id,
            node_type="authority-reference",
            owner_repo=authority_ref["repo"],
            external_ref=f"{authority_ref['repo']}:{authority_ref['path']}@{authority_ref['ref']}",
            properties={
                "digest": authority_ref.get("digest"),
                "freshness_status": authority_ref.get("freshness_status"),
                "manifest_id": manifest["manifest_id"],
                "path": authority_ref["path"],
                "ref": authority_ref["ref"],
                "repo": authority_ref["repo"],
            },
        )
    return nodes


def _repo_nodes(manifest: dict[str, Any]) -> dict[str, ManifestGraphNode]:
    nodes: dict[str, ManifestGraphNode] = {}
    for repo in manifest["repos"]:
        repo_id = repo["repo_id"].strip()
        nodes[repo_id] = _manifest_node(
            prefix="repo",
            raw_id=repo_id,
            node_type="repo",
            owner_repo=repo["owner_repo"],
            external_ref=repo.get("repo_name") or repo_id,
            properties={
                "authority_ref_ids": repo["authority_ref_ids"],
                "manifest_id": manifest["manifest_id"],
                "repo_name": repo.get("repo_name") or repo_id,
            },
        )
    return nodes


def _add_authority_edges(
    add_edge,
    source_node_id: str,
    section_items: list[dict[str, Any]],
    id_field: str,
    entity_id: str,
) -> None:
    item = next(entry for entry in section_items if entry[id_field].strip() == entity_id)
    for authority_id in item["authority_ref_ids"]:
        add_edge(source_node_id, _node_id("authority", authority_id), "declared-by-authority")


def _manifest_node(
    *,
    external_ref: str | None,
    node_type: str,
    owner_repo: str | None,
    prefix: str,
    properties: dict[str, Any],
    raw_id: str,
) -> ManifestGraphNode:
    return ManifestGraphNode(
        external_ref=external_ref,
        node_id=_node_id(prefix, raw_id),
        node_type=node_type,
        owner_repo=owner_repo,
        properties=properties,
    )


def _node_id(prefix: str, raw_id: str) -> str:
    candidate = f"{prefix}:{raw_id.strip()}"
    if len(candidate) <= 128:
        return candidate
    digest = sha256(candidate.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _edge_id(source_node_id: str, edge_type: str, target_node_id: str) -> str:
    digest = sha256(f"{source_node_id}|{edge_type}|{target_node_id}".encode("utf-8")).hexdigest()[:24]
    return f"edge:{digest}"


def _resolve_scope_node_id(scope: str, nodes: dict[str, ManifestGraphNode]) -> str:
    if scope in nodes:
        return scope
    return _node_id("scope", scope)
