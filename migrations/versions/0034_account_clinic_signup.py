"""Account-authorized new clinics and checkout retry identity (additive)."""

import sqlalchemy as sa
from alembic import op

revision = "0034_account_clinic_signup"
down_revision = "0033_staff_session_rotation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("signup_intents", sa.Column("owner_account_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_signup_account", "signup_intents", "staff_accounts", ["owner_account_id"], ["id"]
    )
    op.add_column("signup_intents", sa.Column("operation_id", sa.Uuid(), nullable=True))
    op.add_column("signup_intents", sa.Column("request_hash", sa.String(64), nullable=True))
    op.add_column(
        "signup_intents",
        sa.Column("checkout_attempt", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("signup_intents", sa.Column("checkout_url", sa.String(2048), nullable=True))
    op.create_unique_constraint(
        "uq_signup_account_operation", "signup_intents", ["owner_account_id", "operation_id"]
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM signup_intents WHERE owner_account_id IS NOT NULL)")
    ):
        raise RuntimeError(
            "Personal intents exist: retain account authorization; downgrade is unsafe"
        )
    op.drop_constraint("uq_signup_account_operation", "signup_intents", type_="unique")
    op.drop_constraint("fk_signup_account", "signup_intents", type_="foreignkey")
    for column in (
        "checkout_url",
        "checkout_attempt",
        "request_hash",
        "operation_id",
        "owner_account_id",
    ):
        op.drop_column("signup_intents", column)
