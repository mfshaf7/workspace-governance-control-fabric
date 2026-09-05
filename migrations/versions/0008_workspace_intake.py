"""Immutable Workspace Intake evaluation receipts.

Revision ID: 0008_workspace_intake
Revises: 0007_repository_lifecycle
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0008_workspace_intake"
down_revision = "0007_repository_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_intake_evaluations",
        sa.Column("evaluation_id", sa.String(192), primary_key=True),
        sa.Column("evaluation_digest", sa.String(71), nullable=False),
        sa.Column("receipt_digest", sa.String(71), nullable=False, unique=True),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("receipt", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("workspace_intake_evaluations")
