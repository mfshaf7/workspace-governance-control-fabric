"""Create immutable Delivery ART readiness receipt ledger.

Revision ID: 0003_create_delivery_art_readiness_receipts
Revises: 0002_create_delivery_artifact_registry
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0003_create_delivery_art_readiness_receipts"
down_revision: str | None = "0002_create_delivery_artifact_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


jsonb = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "delivery_art_readiness_receipts",
        sa.Column("receipt_id", sa.String(length=128), nullable=False),
        sa.Column("receipt_uri", sa.String(length=512), nullable=False),
        sa.Column("receipt_digest", sa.String(length=71), nullable=False),
        sa.Column("decision_key", sa.String(length=71), nullable=False),
        sa.Column("delivery_id", sa.String(length=64), nullable=False),
        sa.Column("subject_artifact_type", sa.String(length=64), nullable=False),
        sa.Column("subject_artifact_id", sa.String(length=256), nullable=False),
        sa.Column("subject_digest_kind", sa.String(length=32), nullable=False),
        sa.Column("subject_digest", sa.String(length=71), nullable=False),
        sa.Column("readiness_level", sa.String(length=32), nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("implementation_ref", sa.String(length=40), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
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
        sa.PrimaryKeyConstraint("receipt_id", name=op.f("pk_delivery_art_readiness_receipts")),
        sa.UniqueConstraint(
            "decision_key",
            name=op.f("uq_delivery_art_readiness_receipts_decision_key"),
        ),
        sa.UniqueConstraint(
            "receipt_digest",
            name=op.f("uq_delivery_art_readiness_receipts_receipt_digest"),
        ),
        sa.UniqueConstraint(
            "receipt_uri",
            name=op.f("uq_delivery_art_readiness_receipts_receipt_uri"),
        ),
        sa.UniqueConstraint(
            "delivery_id",
            "subject_artifact_type",
            "subject_artifact_id",
            "readiness_level",
            "profile_id",
            "generation",
            name="uq_delivery_art_readiness_subject_generation",
        ),
        sa.UniqueConstraint(
            "supersedes_receipt_uri",
            name="uq_delivery_art_readiness_supersedes_uri",
        ),
    )
    op.create_index(
        "ix_delivery_art_readiness_subject",
        "delivery_art_readiness_receipts",
        [
            "delivery_id",
            "subject_artifact_type",
            "subject_artifact_id",
            "readiness_level",
            "profile_id",
            "generation",
        ],
    )
    op.create_index(
        "ix_delivery_art_readiness_subject_digest",
        "delivery_art_readiness_receipts",
        ["subject_digest", "readiness_level"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_delivery_art_readiness_subject_digest",
        table_name="delivery_art_readiness_receipts",
    )
    op.drop_index(
        "ix_delivery_art_readiness_subject",
        table_name="delivery_art_readiness_receipts",
    )
    op.drop_table("delivery_art_readiness_receipts")
