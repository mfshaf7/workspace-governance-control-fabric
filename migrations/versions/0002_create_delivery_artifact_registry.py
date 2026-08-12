"""Create the Delivery ART artifact registry and custody receipt tables.

Revision ID: 0002_delivery_art_registry
Revises: 0001_create_foundation_tables
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0002_delivery_art_registry"
down_revision: str | None = "0001_create_foundation_tables"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


jsonb = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "delivery_artifact_registry_entries",
        sa.Column("registry_uri", sa.String(length=256), nullable=False),
        sa.Column("content_digest", sa.String(length=71), nullable=False),
        sa.Column("artifact_type", sa.String(length=64), nullable=False),
        sa.Column("artifact_id", sa.String(length=256), nullable=False),
        sa.Column("delivery_id", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("object_version_id", sa.String(length=256), nullable=False),
        sa.Column("storage_receipt_ref", sa.String(length=512), nullable=False),
        sa.Column("supersedes_registry_uri", sa.String(length=256), nullable=True),
        sa.Column("supersedes_content_digest", sa.String(length=71), nullable=True),
        sa.Column("persisted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("registry_uri", name=op.f("pk_delivery_artifact_registry_entries")),
        sa.UniqueConstraint(
            "content_digest",
            name=op.f("uq_delivery_artifact_registry_entries_content_digest"),
        ),
        sa.UniqueConstraint(
            "delivery_id",
            "artifact_type",
            "artifact_id",
            "generation",
            name="uq_delivery_artifact_registry_subject_generation",
        ),
        sa.UniqueConstraint(
            "supersedes_registry_uri",
            name="uq_delivery_artifact_registry_supersedes_uri",
        ),
    )
    op.create_index(
        "ix_delivery_artifact_registry_subject",
        "delivery_artifact_registry_entries",
        ["delivery_id", "artifact_type", "artifact_id", "generation"],
    )

    op.create_table(
        "delivery_artifact_custody_receipts",
        sa.Column("receipt_id", sa.String(length=128), nullable=False),
        sa.Column("registry_uri", sa.String(length=256), nullable=False),
        sa.Column("receipt_uri", sa.String(length=512), nullable=False),
        sa.Column("receipt_digest", sa.String(length=71), nullable=False),
        sa.Column("receipt", jsonb, nullable=False),
        sa.Column("persisted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["registry_uri"],
            ["delivery_artifact_registry_entries.registry_uri"],
            name="fk_delivery_artifact_custody_registry",
        ),
        sa.PrimaryKeyConstraint("receipt_id", name=op.f("pk_delivery_artifact_custody_receipts")),
        sa.UniqueConstraint(
            "receipt_digest",
            name=op.f("uq_delivery_artifact_custody_receipts_receipt_digest"),
        ),
        sa.UniqueConstraint(
            "receipt_uri",
            name=op.f("uq_delivery_artifact_custody_receipts_receipt_uri"),
        ),
        sa.UniqueConstraint(
            "registry_uri",
            name=op.f("uq_delivery_artifact_custody_receipts_registry_uri"),
        ),
    )
    op.create_index(
        "ix_delivery_artifact_custody_registry_uri",
        "delivery_artifact_custody_receipts",
        ["registry_uri"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_delivery_artifact_custody_registry_uri",
        table_name="delivery_artifact_custody_receipts",
    )
    op.drop_table("delivery_artifact_custody_receipts")
    op.drop_index(
        "ix_delivery_artifact_registry_subject",
        table_name="delivery_artifact_registry_entries",
    )
    op.drop_table("delivery_artifact_registry_entries")
