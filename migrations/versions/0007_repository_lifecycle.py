"""Create the repository lifecycle readiness decision ledger.

Revision ID: 0007_repository_lifecycle
Revises: 0006_repository_custody
Create Date: 2026-08-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0007_repository_lifecycle"
down_revision: str | None = "0006_repository_custody"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


jsonb = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "repository_lifecycle_decisions",
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("decision_uri", sa.String(length=512), nullable=False),
        sa.Column("decision_digest", sa.String(length=71), nullable=False),
        sa.Column("request_id", sa.String(length=192), nullable=False),
        sa.Column("request_digest", sa.String(length=71), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_repository_id", sa.String(length=256), nullable=False),
        sa.Column("workspace_owner_ref", sa.String(length=256), nullable=False),
        sa.Column("custody_version", sa.String(length=256), nullable=False),
        sa.Column("provider_version", sa.String(length=256), nullable=True),
        sa.Column("policy_digest", sa.String(length=71), nullable=False),
        sa.Column("implementation_ref", sa.String(length=40), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("decision", jsonb, nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("decision_id", name=op.f("pk_repository_lifecycle_decisions")),
        sa.UniqueConstraint(
            "decision_digest",
            name=op.f("uq_repository_lifecycle_decisions_decision_digest"),
        ),
        sa.UniqueConstraint(
            "decision_uri",
            name=op.f("uq_repository_lifecycle_decisions_decision_uri"),
        ),
        sa.UniqueConstraint(
            "request_id",
            name=op.f("uq_repository_lifecycle_decisions_request_id"),
        ),
    )
    op.create_index(
        "ix_repository_lifecycle_decision_subject",
        "repository_lifecycle_decisions",
        ["provider", "provider_repository_id", "workspace_owner_ref"],
    )
    op.create_index(
        "ix_repository_lifecycle_decision_policy",
        "repository_lifecycle_decisions",
        ["policy_digest", "outcome"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_repository_lifecycle_decision_policy",
        table_name="repository_lifecycle_decisions",
    )
    op.drop_index(
        "ix_repository_lifecycle_decision_subject",
        table_name="repository_lifecycle_decisions",
    )
    op.drop_table("repository_lifecycle_decisions")
