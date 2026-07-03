"""refresh_tokens (revocable sessions) + processed_stripe_events (webhook idempotency)

Revision ID: 0003_billing_refresh
Revises: 0002_precheck_account_links
Create Date: 2026-07-01 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_billing_refresh"
down_revision: str | None = "0002_precheck_account_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # SHA-256 hex of the opaque token — the raw value is never stored.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_refresh_tokens_token_hash"), "refresh_tokens", ["token_hash"], unique=True
    )

    op.create_table(
        "processed_stripe_events",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("processed_stripe_events")
    op.drop_index(op.f("ix_refresh_tokens_token_hash"), table_name="refresh_tokens")
    op.drop_index(op.f("ix_refresh_tokens_user_id"), table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
