"""Create the Prototype ingress readiness receipt ledger.

Revision ID: 0004_prototype_ingress_ready
Revises: 0003_delivery_art_readiness
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0004_prototype_ingress_ready"
down_revision: str | None = "0003_delivery_art_readiness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


jsonb = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "prototype_ingress_readiness_receipts",
        sa.Column("receipt_id", sa.String(length=128), nullable=False),
        sa.Column("receipt_uri", sa.String(length=512), nullable=False),
        sa.Column("receipt_digest", sa.String(length=71), nullable=False),
        sa.Column("decision_key", sa.String(length=71), nullable=False),
        sa.Column("packet_ref", sa.String(length=512), nullable=False),
        sa.Column("packet_digest", sa.String(length=71), nullable=False),
        sa.Column("source_record_ref", sa.String(length=512), nullable=False),
        sa.Column("source_record_version", sa.String(length=40), nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("contract_digest", sa.String(length=71), nullable=False),
        sa.Column("implementation_ref", sa.String(length=40), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("receipt", jsonb, nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("persisted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("supersedes_receipt_uri", sa.String(length=512), nullable=True),
        sa.Column("supersedes_receipt_digest", sa.String(length=71), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "receipt_id",
            name=op.f("pk_prototype_ingress_readiness_receipts"),
        ),
        sa.UniqueConstraint("decision_key", name=op.f("uq_prototype_ingress_readiness_receipts_decision_key")),
        sa.UniqueConstraint("receipt_digest", name=op.f("uq_prototype_ingress_readiness_receipts_receipt_digest")),
        sa.UniqueConstraint("receipt_uri", name=op.f("uq_prototype_ingress_readiness_receipts_receipt_uri")),
        sa.UniqueConstraint(
            "source_record_ref",
            "source_record_version",
            "profile_id",
            "generation",
            name="uq_prototype_ingress_readiness_source_generation",
        ),
        sa.UniqueConstraint(
            "supersedes_receipt_uri",
            name="uq_prototype_ingress_readiness_supersedes_uri",
        ),
    )
    op.create_index(
        "ix_prototype_ingress_readiness_source",
        "prototype_ingress_readiness_receipts",
        ["source_record_ref", "source_record_version", "profile_id", "generation"],
    )
    op.create_index(
        "ix_prototype_ingress_readiness_packet",
        "prototype_ingress_readiness_receipts",
        ["packet_digest", "profile_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_prototype_ingress_readiness_packet",
        table_name="prototype_ingress_readiness_receipts",
    )
    op.drop_index(
        "ix_prototype_ingress_readiness_source",
        table_name="prototype_ingress_readiness_receipts",
    )
    op.drop_table("prototype_ingress_readiness_receipts")
