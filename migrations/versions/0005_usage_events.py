"""usage_events — internal usage-event ledger (metering leg)

Revision ID: 0005_usage_events
Revises: 0004_privacy_requests
Create Date: 2026-07-02 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_usage_events"
down_revision: str | None = "0004_privacy_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        # Caller-supplied idempotency key, e.g. "reminder:24h:<appointment_id>".
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("feature", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_usage_events_tenant_id"), "usage_events", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_usage_events_tenant_id"), table_name="usage_events")
    op.drop_table("usage_events")
