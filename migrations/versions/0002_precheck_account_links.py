"""precheck_account_links — brain user (UUID) <-> PreCheck user (int) SSO link

Revision ID: 0002_precheck_account_links
Revises: 0001_initial_schema
Create Date: 2026-06-18 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_precheck_account_links"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "precheck_account_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("brain_user_id", sa.Uuid(), nullable=False),
        # Logical reference to precheck.users.id (a SEPARATE database) — no FK by design.
        sa.Column("precheck_user_id", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["brain_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One brain user -> at most one PreCheck user (mandated). Reverse unique on
        # precheck_user_id is defensive (no two brain users share a PreCheck identity).
        sa.UniqueConstraint("brain_user_id", name="uq_precheck_links_brain_user"),
        sa.UniqueConstraint("precheck_user_id", name="uq_precheck_links_precheck_user"),
    )
    op.create_index(
        op.f("ix_precheck_account_links_tenant_id"),
        "precheck_account_links",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_precheck_account_links_tenant_id"),
        table_name="precheck_account_links",
    )
    op.drop_table("precheck_account_links")
