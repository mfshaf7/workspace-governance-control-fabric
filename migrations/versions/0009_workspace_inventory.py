"""Immutable Workspace active-inventory readiness artifacts.

Revision ID: 0009_workspace_inventory
Revises: 0008_workspace_intake
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0009_workspace_inventory"
down_revision = "0008_workspace_intake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_inventory_evaluations",
        sa.Column("evaluation_id", sa.String(192), primary_key=True),
        sa.Column("evaluation_digest", sa.String(71), nullable=False),
        sa.Column("readiness_digest", sa.String(71), nullable=False, unique=True),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("readiness", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("workspace_inventory_evaluations")
