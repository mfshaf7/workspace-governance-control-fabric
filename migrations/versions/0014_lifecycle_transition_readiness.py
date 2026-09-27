"""Immutable lifecycle-transition readiness and escalation evidence.

Revision ID: 0014_lifecycle_readiness
Revises: 0013_prototype_closure
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0014_lifecycle_readiness"
down_revision = "0013_prototype_closure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lifecycle_transition_readiness",
        sa.Column("evaluation_id", sa.String(192), primary_key=True),
        sa.Column("evaluation_digest", sa.String(71), nullable=False),
        sa.Column("readiness_digest", sa.String(71), nullable=False, unique=True),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("transition_id", sa.String(256), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False),
        sa.Column("contract_digest", sa.String(71), nullable=False),
        sa.Column("implementation_ref", sa.String(40), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
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
    op.drop_table("lifecycle_transition_readiness")
