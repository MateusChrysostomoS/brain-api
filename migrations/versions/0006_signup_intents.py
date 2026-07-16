"""signup_intents — cold-signup -> Stripe checkout -> auto-provisioning bridge

Revision ID: 0006_signup_intents
Revises: 0005_usage_events
Create Date: 2026-07-16 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_signup_intents"
down_revision: str | None = "0005_usage_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signup_intents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("clinic_name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("whatsapp_phone", sa.String(length=32), nullable=False),
        sa.Column("catalog_ids", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending_payment", nullable=False),
        sa.Column("failure_reason", sa.String(length=255), nullable=True),
        sa.Column("stripe_session_id", sa.String(length=255), nullable=True),
        sa.Column("stripe_customer_id", sa.String(length=64), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(length=64), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("onboarding_token_hash", sa.String(length=64), nullable=True),
        sa.Column("onboarding_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("onboarding_token_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_signup_intents_stripe_session_id"),
        "signup_intents",
        ["stripe_session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_signup_intents_onboarding_token_hash"),
        "signup_intents",
        ["onboarding_token_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_signup_intents_onboarding_token_hash"), table_name="signup_intents")
    op.drop_index(op.f("ix_signup_intents_stripe_session_id"), table_name="signup_intents")
    op.drop_table("signup_intents")
