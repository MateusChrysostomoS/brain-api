"""precheck_topup_credits + usage_events composite index (precheck-billing round)

Revision ID: 0010_precheck_billing
Revises: 0009_test_window
Create Date: 2026-08-01 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_precheck_billing"
down_revision: str | None = "0009_test_window"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "precheck_topup_credits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("amount_total_cents", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("stripe_checkout_session_id", sa.String(length=255), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_precheck_topup_credits_tenant_id"),
        "precheck_topup_credits",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_precheck_topup_credits_expires_at"),
        "precheck_topup_credits",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_precheck_topup_credits_stripe_checkout_session_id"),
        "precheck_topup_credits",
        ["stripe_checkout_session_id"],
        unique=True,
    )

    # Composite index for the usage-window SUM query (services/precheck_billing.py::
    # usage_summary) — a tenant + feature + created_at RANGE scan, the exact access
    # pattern every PreCheck quota check performs.
    op.create_index(
        "ix_usage_events_tenant_feature_created",
        "usage_events",
        ["tenant_id", "feature", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_usage_events_tenant_feature_created", table_name="usage_events")
    op.drop_index(
        op.f("ix_precheck_topup_credits_stripe_checkout_session_id"),
        table_name="precheck_topup_credits",
    )
    op.drop_index(
        op.f("ix_precheck_topup_credits_expires_at"), table_name="precheck_topup_credits"
    )
    op.drop_index(
        op.f("ix_precheck_topup_credits_tenant_id"), table_name="precheck_topup_credits"
    )
    op.drop_table("precheck_topup_credits")
