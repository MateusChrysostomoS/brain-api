"""onboarding state machine, signup_attempts ledger, professional linkage + invite tokens

Revision ID: 0007_onboarding_state_machine
Revises: 0006_signup_intents
Create Date: 2026-07-18 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_onboarding_state_machine"
down_revision: str | None = "0006_signup_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- tenants: onboarding state machine (CONTRACT_onboarding_v1.md §2, §8) ---------
    op.add_column(
        "tenants",
        sa.Column(
            "onboarding_state", sa.String(length=40), server_default="pending", nullable=False
        ),
    )
    op.add_column("tenants", sa.Column("blocker_reason", sa.String(length=40), nullable=True))
    op.add_column(
        "tenants",
        sa.Column(
            "config_status", sa.String(length=40), server_default="incompleta", nullable=False
        ),
    )
    op.add_column(
        "tenants", sa.Column("onboarding_anchor_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("tenants", sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenants", sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenants", sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "tenants",
        sa.Column("secretaria_provisioned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("config_reminder_anchor_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenants", sa.Column("last_config_reminder_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "tenants", sa.Column("closing_email_sent_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "tenants",
        sa.Column("manual_review_flagged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("retry_paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "config_reminder_paused",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )

    # Backfill: every tenant that already existed is, by definition, already onboarded —
    # only a tenant provisioned FROM NOW ON should ever start at 'pending'/'incompleta'
    # (services.onboarding.provision_defaults). Safe to target the whole table: no new
    # tenant can have been inserted mid-migration.
    op.execute("UPDATE tenants SET onboarding_state = 'ativo'")
    op.execute("UPDATE tenants SET config_status = 'completa'")

    # --- signup_attempts: idempotency ledger for onboarding connection attempts -------
    op.create_table(
        "signup_attempts",
        # Client-supplied attempt id — the PK IS the idempotency key (no server-generated
        # id); a replay of the same id is a no-op, never a duplicate row.
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        # user | retry_nudge | intake
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("day_offset", sa.Integer(), nullable=True),
        # pass | fail
        sa.Column("result", sa.String(length=10), nullable=False),
        sa.Column("blocker_reason", sa.String(length=40), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_signup_attempts_tenant_id"), "signup_attempts", ["tenant_id"], unique=False
    )

    # --- users: cross-service professional linkage + invite tokens --------------------
    op.add_column(
        "users",
        # Cross-service value reference to secretaria.professionals.id — NO ForeignKey by
        # design (the row lives in secretaria's own database; same convention as
        # tenant_id-style cross-service references elsewhere in the mesh).
        sa.Column("professional_id", sa.Uuid(), nullable=True),
    )
    op.add_column("users", sa.Column("invite_token_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "users", sa.Column("invite_token_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        op.f("ix_users_invite_token_hash"), "users", ["invite_token_hash"], unique=False
    )

    # --- entitlements: Stripe trial-hardening marker (CONTRACT_onboarding_v1.md §8) ---
    op.add_column(
        "entitlements", sa.Column("charge_hardened_at", sa.DateTime(timezone=True), nullable=True)
    )

    # --- signup_intents: raw pre-checkout intake answers (§7) -------------------------
    op.add_column("signup_intents", sa.Column("intake", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("signup_intents", "intake")

    op.drop_column("entitlements", "charge_hardened_at")

    op.drop_index(op.f("ix_users_invite_token_hash"), table_name="users")
    op.drop_column("users", "invite_token_expires_at")
    op.drop_column("users", "invite_token_hash")
    op.drop_column("users", "professional_id")

    op.drop_index(op.f("ix_signup_attempts_tenant_id"), table_name="signup_attempts")
    op.drop_table("signup_attempts")

    op.drop_column("tenants", "config_reminder_paused")
    op.drop_column("tenants", "retry_paused")
    op.drop_column("tenants", "manual_review_flagged_at")
    op.drop_column("tenants", "closing_email_sent_at")
    op.drop_column("tenants", "last_config_reminder_at")
    op.drop_column("tenants", "config_reminder_anchor_at")
    op.drop_column("tenants", "secretaria_provisioned_at")
    op.drop_column("tenants", "activated_at")
    op.drop_column("tenants", "connected_at")
    op.drop_column("tenants", "next_retry_at")
    op.drop_column("tenants", "onboarding_anchor_at")
    op.drop_column("tenants", "config_status")
    op.drop_column("tenants", "blocker_reason")
    op.drop_column("tenants", "onboarding_state")
