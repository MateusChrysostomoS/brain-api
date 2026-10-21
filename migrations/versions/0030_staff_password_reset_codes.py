"""Staff password reset by e-mailed code (TASK-056): additive table only."""

import sqlalchemy as sa
from alembic import op

revision = "0030_staff_password_reset_codes"
down_revision = "0029_staff_first_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "staff_password_reset_codes",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_in_window", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("staff_password_reset_codes")
