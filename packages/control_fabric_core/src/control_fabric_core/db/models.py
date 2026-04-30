"""Policy-neutral persistence model for graph, receipt, and ledger records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, MetaData, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


metadata = MetaData(naming_convention={
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})


json_payload = JSONB().with_variant(JSON(), "sqlite")


class Base(DeclarativeBase):
    metadata = metadata


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class GovernanceNode(TimestampMixin, Base):
    """Node in the fabric-local governance graph projection."""

    __tablename__ = "governance_nodes"

    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    node_type: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_repo: Mapped[str | None] = mapped_column(String(128))
    external_ref: Mapped[str | None] = mapped_column(String(512))
    properties: Mapped[dict[str, Any]] = mapped_column(json_payload, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    outgoing_edges: Mapped[list[GovernanceEdge]] = relationship(
        back_populates="source_node",
        cascade="all, delete-orphan",
        foreign_keys="GovernanceEdge.source_node_id",
    )
    incoming_edges: Mapped[list[GovernanceEdge]] = relationship(
        back_populates="target_node",
        cascade="all, delete-orphan",
        foreign_keys="GovernanceEdge.target_node_id",
    )


class GovernanceEdge(TimestampMixin, Base):
    """Directed relationship between two governance graph nodes."""

    __tablename__ = "governance_edges"

    edge_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_node_id: Mapped[str] = mapped_column(ForeignKey("governance_nodes.node_id"), nullable=False)
    target_node_id: Mapped[str] = mapped_column(ForeignKey("governance_nodes.node_id"), nullable=False)
    edge_type: Mapped[str] = mapped_column(String(64), nullable=False)
    properties: Mapped[dict[str, Any]] = mapped_column(json_payload, nullable=False, default=dict)

    source_node: Mapped[GovernanceNode] = relationship(
        back_populates="outgoing_edges",
        foreign_keys=[source_node_id],
    )
    target_node: Mapped[GovernanceNode] = relationship(
        back_populates="incoming_edges",
        foreign_keys=[target_node_id],
    )


class AuthorityReference(TimestampMixin, Base):
    """Digest-linked reference to an upstream authority source."""

    __tablename__ = "authority_references"

    authority_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    repo: Mapped[str] = mapped_column(String(128), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    ref: Mapped[str] = mapped_column(String(128), nullable=False)
    digest: Mapped[str] = mapped_column(String(128), nullable=False)
    freshness_status: Mapped[str] = mapped_column(String(32), nullable=False)


class SourceSnapshot(TimestampMixin, Base):
    """Immutable source snapshot consumed by a validation plan."""

    __tablename__ = "source_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    source_roots: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)
    authority_refs: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)
    digests: Mapped[dict[str, Any]] = mapped_column(json_payload, nullable=False, default=dict)
    excluded_refs: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)


class ValidationPlan(TimestampMixin, Base):
    """Planned validation checks for a target before execution."""

    __tablename__ = "validation_plans"

    plan_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("source_snapshots.snapshot_id"), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(256), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    planned_checks: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)
    required_authority_refs: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)
    blocked_controls: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)


class ValidationRun(Base):
    """Executed validation-plan result with bounded artifact references."""

    __tablename__ = "validation_runs"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("validation_plans.plan_id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    check_results: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)
    artifact_refs: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)
    receipt_id: Mapped[str | None] = mapped_column(String(128), unique=True)


class ControlReceipt(TimestampMixin, Base):
    """Compact operator-safe proof packet for one fabric action."""

    __tablename__ = "control_receipts"

    receipt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_snapshot_id: Mapped[str] = mapped_column(ForeignKey("source_snapshots.snapshot_id"), nullable=False)
    target: Mapped[str] = mapped_column(String(256), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    findings: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)
    suppressed_output_summary: Mapped[dict[str, Any]] = mapped_column(json_payload, nullable=False, default=dict)
    artifact_refs: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)
    next_required_action: Mapped[dict[str, Any]] = mapped_column(json_payload, nullable=False, default=dict)


class ReadinessDecision(TimestampMixin, Base):
    """Explicit readiness decision for one target under one profile."""

    __tablename__ = "readiness_decisions"

    decision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    target: Mapped[str] = mapped_column(String(256), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)
    authority_refs: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)
    receipt_refs: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)
    escalation_path_when_blocked: Mapped[dict[str, Any]] = mapped_column(json_payload, nullable=False, default=dict)


class LedgerEvent(Base):
    """Append-only audit event for source, validation, readiness, and receipt actions."""

    __tablename__ = "ledger_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    target: Mapped[str] = mapped_column(String(256), nullable=False)
    source_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("source_snapshots.snapshot_id"))
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    receipt_refs: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)


class EscalationRecord(TimestampMixin, Base):
    """Bounded handoff record for blockers routed outside the fabric."""

    __tablename__ = "escalation_records"

    escalation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    trigger_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_system: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_repo: Mapped[str] = mapped_column(String(128), nullable=False)
    required_record: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(json_payload, nullable=False, default=list)
    operator_action_required: Mapped[str] = mapped_column(Text, nullable=False)


Index("ix_governance_nodes_type_owner", GovernanceNode.node_type, GovernanceNode.owner_repo)
Index("ix_governance_edges_source_type", GovernanceEdge.source_node_id, GovernanceEdge.edge_type)
Index("ix_governance_edges_target_type", GovernanceEdge.target_node_id, GovernanceEdge.edge_type)
Index("ix_authority_references_repo_path", AuthorityReference.repo, AuthorityReference.path)
Index("ix_validation_plans_target_profile", ValidationPlan.target, ValidationPlan.profile_id)
Index("ix_validation_runs_plan_status", ValidationRun.plan_id, ValidationRun.status)
Index("ix_control_receipts_target_outcome", ControlReceipt.target, ControlReceipt.outcome)
Index("ix_readiness_decisions_target_profile", ReadinessDecision.target, ReadinessDecision.profile_id)
Index("ix_ledger_events_target_action", LedgerEvent.target, LedgerEvent.action)
Index("ix_escalation_records_trigger_owner", EscalationRecord.trigger_id, EscalationRecord.owner_repo)
