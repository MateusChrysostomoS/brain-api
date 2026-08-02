"""waitlist_leads — pre-launch "avise-me quando lançar" capture (launch-gate round)

Revision ID: 0011_launch_waitlist
Revises: 0010_precheck_billing
Create Date: 2026-08-01 00:00:00.000000

Additive and isolated: one new table, no FKs, no change to any existing table. The
UNIQUE index on `email` is what makes POST /public/launch-waitlist idempotent (the
service lowercases before insert, so the constraint dedupes case variants too).

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_launch_waitlist"
down_revision: str | None = "0010_precheck_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "waitlist_leads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("plan_hint", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_waitlist_leads_email"), "waitlist_leads", ["email"], unique=True)
    op.create_index(
        op.f("ix_waitlist_leads_created_at"), "waitlist_leads", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_waitlist_leads_created_at"), table_name="waitlist_leads")
    op.drop_index(op.f("ix_waitlist_leads_email"), table_name="waitlist_leads")
    op.drop_table("waitlist_leads")
