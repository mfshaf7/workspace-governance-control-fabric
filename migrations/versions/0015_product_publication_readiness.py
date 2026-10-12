"""Immutable Product Publication readiness evidence.

Revision ID: 0015_product_publication
Revises: 0014_lifecycle_readiness
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0015_product_publication"
down_revision = "0014_lifecycle_readiness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_publication_readiness",
        sa.Column("evaluation_id", sa.String(192), primary_key=True),
        sa.Column("evaluation_digest", sa.String(71), nullable=False),
        sa.Column("readiness_digest", sa.String(71), nullable=False, unique=True),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("request_id", sa.String(192), nullable=False),
        sa.Column("idempotency_key", sa.String(512), nullable=False),
        sa.Column("request_digest", sa.String(71), nullable=False),
        sa.Column("authority_revision", sa.String(40), nullable=False),
        sa.Column("contract_digest", sa.String(71), nullable=False),
        sa.Column("implementation_ref", sa.String(40), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("readiness", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "actor",
            "idempotency_key",
            name="uq_product_publication_actor_idempotency",
        ),
        sa.UniqueConstraint(
            "actor",
            "request_id",
            name="uq_product_publication_actor_request",
        ),
    )


def downgrade() -> None:
    op.drop_table("product_publication_readiness")
