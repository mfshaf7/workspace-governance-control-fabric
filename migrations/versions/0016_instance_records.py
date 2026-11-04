"""Private installation operational records, immutable revisions and replay.

Revision ID: 0016_instance_records
Revises: 0015_product_publication
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0016_instance_records"
down_revision = "0015_product_publication"
branch_labels = None
depends_on = None


def _created():
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.create_table(
        "instance_operational_records",
        sa.Column("instance_id", sa.String(128), primary_key=True),
        sa.Column("record_id", sa.String(192), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("record_digest", sa.String(71), nullable=False),
        sa.Column("provider", sa.String(32)),
        sa.Column("provider_repository_id", sa.String(32)),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        _created(),
        sa.UniqueConstraint("instance_id", "provider", "provider_repository_id",
                            name="uq_instance_record_provider_identity"),
    )
    op.create_table(
        "instance_operational_revisions",
        sa.Column("instance_id", sa.String(128), primary_key=True),
        sa.Column("record_id", sa.String(192), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("receipt", postgresql.JSONB(), nullable=False),
        _created(),
    )
    op.create_table(
        "instance_operational_operations",
        sa.Column("instance_id", sa.String(128), primary_key=True),
        sa.Column("idempotency_key", sa.String(256), primary_key=True),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("ai_decision_id", sa.String(256)),
        sa.Column("command_digest", sa.String(71), nullable=False),
        sa.Column("command", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        _created(),
        sa.UniqueConstraint("instance_id", "ai_decision_id", name="uq_instance_operation_ai_decision"),
    )


def downgrade() -> None:
    op.drop_table("instance_operational_operations")
    op.drop_table("instance_operational_revisions")
    op.drop_table("instance_operational_records")
