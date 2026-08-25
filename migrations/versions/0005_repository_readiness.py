"""Create the repository readiness receipt ledger.

Revision ID: 0005_repository_readiness
Revises: 0004_prototype_ingress_ready
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0005_repository_readiness"
down_revision: str | None = "0004_prototype_ingress_ready"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


jsonb = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "repository_readiness_receipts",
        sa.Column("receipt_id", sa.String(length=128), nullable=False),
        sa.Column("receipt_uri", sa.String(length=512), nullable=False),
        sa.Column("receipt_digest", sa.String(length=71), nullable=False),
        sa.Column("decision_key", sa.String(length=71), nullable=False),
        sa.Column("repo_name", sa.String(length=128), nullable=False),
        sa.Column("repo_ref", sa.String(length=256), nullable=False),
        sa.Column("catalog_value_key", sa.String(length=128), nullable=False),
        sa.Column("authority_digest", sa.String(length=71), nullable=False),
        sa.Column("rule_digest", sa.String(length=71), nullable=True),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("contract_digest", sa.String(length=71), nullable=False),
        sa.Column("implementation_ref", sa.String(length=40), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("receipt", jsonb, nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("persisted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("supersedes_receipt_uri", sa.String(length=512), nullable=True),
        sa.Column("supersedes_receipt_digest", sa.String(length=71), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("receipt_id", name=op.f("pk_repository_readiness_receipts")),
        sa.UniqueConstraint("decision_key", name=op.f("uq_repository_readiness_receipts_decision_key")),
        sa.UniqueConstraint("receipt_digest", name=op.f("uq_repository_readiness_receipts_receipt_digest")),
        sa.UniqueConstraint("receipt_uri", name=op.f("uq_repository_readiness_receipts_receipt_uri")),
        sa.UniqueConstraint(
            "repo_name",
            "catalog_value_key",
            "profile_id",
            "generation",
            name="uq_repository_readiness_subject_generation",
        ),
        sa.UniqueConstraint(
            "supersedes_receipt_uri",
            name="uq_repository_readiness_supersedes_uri",
        ),
    )
    op.create_index(
        "ix_repository_readiness_subject",
        "repository_readiness_receipts",
        ["repo_name", "catalog_value_key", "profile_id", "generation"],
    )
    op.create_index(
        "ix_repository_readiness_authority",
        "repository_readiness_receipts",
        ["authority_digest", "profile_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_repository_readiness_authority", table_name="repository_readiness_receipts")
    op.drop_index("ix_repository_readiness_subject", table_name="repository_readiness_receipts")
    op.drop_table("repository_readiness_receipts")
