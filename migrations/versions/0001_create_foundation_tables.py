"""Create graph, receipt, and ledger foundation tables.

Revision ID: 0001_create_foundation_tables
Revises:
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0001_create_foundation_tables"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


jsonb = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "governance_nodes",
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("node_type", sa.String(length=64), nullable=False),
        sa.Column("owner_repo", sa.String(length=128), nullable=True),
        sa.Column("external_ref", sa.String(length=512), nullable=True),
        sa.Column("properties", jsonb, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("node_id", name=op.f("pk_governance_nodes")),
    )
    op.create_index("ix_governance_nodes_type_owner", "governance_nodes", ["node_type", "owner_repo"])

    op.create_table(
        "authority_references",
        sa.Column("authority_id", sa.String(length=128), nullable=False),
        sa.Column("repo", sa.String(length=128), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("ref", sa.String(length=128), nullable=False),
        sa.Column("digest", sa.String(length=128), nullable=False),
        sa.Column("freshness_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("authority_id", name=op.f("pk_authority_references")),
    )
    op.create_index("ix_authority_references_repo_path", "authority_references", ["repo", "path"])

    op.create_table(
        "source_snapshots",
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("source_roots", jsonb, nullable=False),
        sa.Column("authority_refs", jsonb, nullable=False),
        sa.Column("digests", jsonb, nullable=False),
        sa.Column("excluded_refs", jsonb, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_source_snapshots")),
    )

    op.create_table(
        "governance_edges",
        sa.Column("edge_id", sa.String(length=128), nullable=False),
        sa.Column("source_node_id", sa.String(length=128), nullable=False),
        sa.Column("target_node_id", sa.String(length=128), nullable=False),
        sa.Column("edge_type", sa.String(length=64), nullable=False),
        sa.Column("properties", jsonb, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["source_node_id"], ["governance_nodes.node_id"], name=op.f("fk_governance_edges_source_node_id_governance_nodes")),
        sa.ForeignKeyConstraint(["target_node_id"], ["governance_nodes.node_id"], name=op.f("fk_governance_edges_target_node_id_governance_nodes")),
        sa.PrimaryKeyConstraint("edge_id", name=op.f("pk_governance_edges")),
    )
    op.create_index("ix_governance_edges_source_type", "governance_edges", ["source_node_id", "edge_type"])
    op.create_index("ix_governance_edges_target_type", "governance_edges", ["target_node_id", "edge_type"])

    op.create_table(
        "validation_plans",
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=256), nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("planned_checks", jsonb, nullable=False),
        sa.Column("required_authority_refs", jsonb, nullable=False),
        sa.Column("blocked_controls", jsonb, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["source_snapshots.snapshot_id"], name=op.f("fk_validation_plans_snapshot_id_source_snapshots")),
        sa.PrimaryKeyConstraint("plan_id", name=op.f("pk_validation_plans")),
    )
    op.create_index("ix_validation_plans_target_profile", "validation_plans", ["target", "profile_id"])

    op.create_table(
        "control_receipts",
        sa.Column("receipt_id", sa.String(length=128), nullable=False),
        sa.Column("source_snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("target", sa.String(length=256), nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("findings", jsonb, nullable=False),
        sa.Column("suppressed_output_summary", jsonb, nullable=False),
        sa.Column("artifact_refs", jsonb, nullable=False),
        sa.Column("next_required_action", jsonb, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["source_snapshot_id"], ["source_snapshots.snapshot_id"], name=op.f("fk_control_receipts_source_snapshot_id_source_snapshots")),
        sa.PrimaryKeyConstraint("receipt_id", name=op.f("pk_control_receipts")),
    )
    op.create_index("ix_control_receipts_target_outcome", "control_receipts", ["target", "outcome"])

    op.create_table(
        "readiness_decisions",
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("target", sa.String(length=256), nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("reasons", jsonb, nullable=False),
        sa.Column("authority_refs", jsonb, nullable=False),
        sa.Column("receipt_refs", jsonb, nullable=False),
        sa.Column("escalation_path_when_blocked", jsonb, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("decision_id", name=op.f("pk_readiness_decisions")),
    )
    op.create_index("ix_readiness_decisions_target_profile", "readiness_decisions", ["target", "profile_id"])

    op.create_table(
        "validation_runs",
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("check_results", jsonb, nullable=False),
        sa.Column("artifact_refs", jsonb, nullable=False),
        sa.Column("receipt_id", sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(["plan_id"], ["validation_plans.plan_id"], name=op.f("fk_validation_runs_plan_id_validation_plans")),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_validation_runs")),
        sa.UniqueConstraint("receipt_id", name=op.f("uq_validation_runs_receipt_id")),
    )
    op.create_index("ix_validation_runs_plan_status", "validation_runs", ["plan_id", "status"])

    op.create_table(
        "ledger_events",
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("target", sa.String(length=256), nullable=False),
        sa.Column("source_snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("receipt_refs", jsonb, nullable=False),
        sa.ForeignKeyConstraint(["source_snapshot_id"], ["source_snapshots.snapshot_id"], name=op.f("fk_ledger_events_source_snapshot_id_source_snapshots")),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_ledger_events")),
    )
    op.create_index("ix_ledger_events_target_action", "ledger_events", ["target", "action"])

    op.create_table(
        "escalation_records",
        sa.Column("escalation_id", sa.String(length=128), nullable=False),
        sa.Column("trigger_id", sa.String(length=128), nullable=False),
        sa.Column("target_system", sa.String(length=128), nullable=False),
        sa.Column("owner_repo", sa.String(length=128), nullable=False),
        sa.Column("required_record", sa.String(length=128), nullable=False),
        sa.Column("evidence_refs", jsonb, nullable=False),
        sa.Column("operator_action_required", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("escalation_id", name=op.f("pk_escalation_records")),
    )
    op.create_index("ix_escalation_records_trigger_owner", "escalation_records", ["trigger_id", "owner_repo"])


def downgrade() -> None:
    op.drop_index("ix_escalation_records_trigger_owner", table_name="escalation_records")
    op.drop_table("escalation_records")
    op.drop_index("ix_ledger_events_target_action", table_name="ledger_events")
    op.drop_table("ledger_events")
    op.drop_index("ix_validation_runs_plan_status", table_name="validation_runs")
    op.drop_table("validation_runs")
    op.drop_index("ix_readiness_decisions_target_profile", table_name="readiness_decisions")
    op.drop_table("readiness_decisions")
    op.drop_index("ix_control_receipts_target_outcome", table_name="control_receipts")
    op.drop_table("control_receipts")
    op.drop_index("ix_validation_plans_target_profile", table_name="validation_plans")
    op.drop_table("validation_plans")
    op.drop_index("ix_governance_edges_target_type", table_name="governance_edges")
    op.drop_index("ix_governance_edges_source_type", table_name="governance_edges")
    op.drop_table("governance_edges")
    op.drop_table("source_snapshots")
    op.drop_index("ix_authority_references_repo_path", table_name="authority_references")
    op.drop_table("authority_references")
    op.drop_index("ix_governance_nodes_type_owner", table_name="governance_nodes")
    op.drop_table("governance_nodes")
