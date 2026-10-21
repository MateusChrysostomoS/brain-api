"""Restricted staff first access and revocable clinic access; existing users unchanged."""

import sqlalchemy as sa
from alembic import op

revision = "0029_staff_first_access"
down_revision = "0028_patient_wrote_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "requires_password_change",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column("temporary_password_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users", sa.Column("credential_version", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "users", sa.Column("access_revoked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "users",
        sa.Column(
            "invite_delivery_status", sa.String(32), nullable=False, server_default="not_requested"
        ),
    )


def downgrade() -> None:
    for column in (
        "invite_delivery_status",
        "access_revoked_at",
        "credential_version",
        "temporary_password_expires_at",
        "requires_password_change",
    ):
        op.drop_column("users", column)
