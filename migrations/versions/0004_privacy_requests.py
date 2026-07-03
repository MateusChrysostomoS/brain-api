"""privacy_requests — LGPD cross-database erasure/export audit trail

Revision ID: 0004_privacy_requests
Revises: 0003_billing_refresh
Create Date: 2026-07-02 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_privacy_requests"
down_revision: str | None = "0003_billing_refresh"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "privacy_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("subject_type", sa.String(length=16), nullable=False),
        # sha256 hex of the normalized subject key — NEVER the raw email/wa_id.
        sa.Column("subject_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        # Counts + per-service status ONLY — never personal data.
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_privacy_requests_subject_hash"),
        "privacy_requests",
        ["subject_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_privacy_requests_subject_hash"), table_name="privacy_requests")
    op.drop_table("privacy_requests")
