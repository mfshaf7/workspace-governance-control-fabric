"""Immutable Prototype Landing readiness artifacts.

Revision ID: 0011_prototype_landing
Revises: 0010_inventory_lifecycle
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0011_prototype_landing"
down_revision = "0010_inventory_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prototype_landing_readiness",
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
    op.drop_table("prototype_landing_readiness")
