"""Core foundation for the Workspace Governance Control Fabric."""

from .foundation import (
    AUTHORITY_CONTRACT_REF,
    PACKAGE_NAME,
    PACKAGE_VERSION,
    RUNTIME_REPO,
    STATUS_BOOTSTRAP,
    status_snapshot,
)
from .graph_ingestion import (
    ManifestGraph,
    ManifestGraphEdge,
    ManifestGraphNode,
    build_manifest_graph,
)
from .graph_queries import (
    ManifestGraphQueryResult,
    build_graph_from_manifest_file,
    graph_summary,
    load_governance_manifest_file,
    query_manifest_file,
    query_manifest_graph,
)
from .database import DATABASE_URL_ENV, DEFAULT_DATABASE_URL, database_settings
from .manifests import (
    MANIFEST_SCHEMA_VERSION,
    ManifestValidationResult,
    governance_manifest_schema,
    manifest_entity_ids,
    validate_governance_manifest,
)
from .worker import worker_status_snapshot

__all__ = [
    "AUTHORITY_CONTRACT_REF",
    "DATABASE_URL_ENV",
    "DEFAULT_DATABASE_URL",
    "MANIFEST_SCHEMA_VERSION",
    "PACKAGE_NAME",
    "PACKAGE_VERSION",
    "RUNTIME_REPO",
    "STATUS_BOOTSTRAP",
    "ManifestGraph",
    "ManifestGraphEdge",
    "ManifestGraphNode",
    "ManifestGraphQueryResult",
    "ManifestValidationResult",
    "build_graph_from_manifest_file",
    "build_manifest_graph",
    "database_settings",
    "graph_summary",
    "governance_manifest_schema",
    "load_governance_manifest_file",
    "manifest_entity_ids",
    "query_manifest_file",
    "query_manifest_graph",
    "status_snapshot",
    "validate_governance_manifest",
    "worker_status_snapshot",
]
