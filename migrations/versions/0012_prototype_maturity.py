"""Immutable Prototype maturity readiness artifacts.

Revision ID: 0012_prototype_maturity
Revises: 0011_prototype_landing
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0012_prototype_maturity"
down_revision = "0011_prototype_landing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prototype_maturity_readiness",
        sa.Column("evaluation_id", sa.String(192), primary_key=True),
        sa.Column("evaluation_digest", sa.String(71), nullable=False),
        sa.Column("readiness_digest", sa.String(71), nullable=False, unique=True),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("authority_revision", sa.String(40), nullable=False),
        sa.Column("contract_digest", sa.String(71), nullable=False),
        sa.Column("implementation_ref", sa.String(40), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("readiness", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("prototype_maturity_readiness")
