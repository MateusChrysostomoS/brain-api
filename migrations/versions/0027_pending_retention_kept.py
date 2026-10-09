"""message_pending_sessions.retention_kept_at: the visit-retention job's "keep" marker

Revision ID: 0027_pending_retention_kept
Revises: 0026_entitlement_product_state
Create Date: 2026-10-09 00:00:00.000000

TASK-042 (`docs/CHECKPOINT_retencao_visitas_portal.md`): a Portal visit that only opened the
link is deleted after 24 h, but only after secretarIA confirms its side is empty too. When
secretarIA answers that the patient DID write (409) or that it never heard of the visit
(`absent`, e.g. a PreCheck-only visit this job cannot inspect), the visit is kept for good
and stamped here, so the next rounds stop asking about it and the batch keeps moving.
`retention_discard_requested_at` is the intent stamp written BEFORE secretarIA is asked: a
later `absent` for a visit that carries it means secretarIA already deleted its side (lost
response, failed local delete), so brain-api finishes the job instead of keeping it forever.

Purely additive and nullable: the previous brain-api neither maps nor reads it, so a code
rollback is safe. Must run BEFORE the brain-api that maps the column (the ORM selects it on
every pending-session read). The index serves the job's age filter.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0027_pending_retention_kept"
down_revision: str | None = "0026_entitlement_product_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "message_pending_sessions",
        sa.Column("retention_kept_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "message_pending_sessions",
        sa.Column("retention_discard_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_message_pending_sessions_created_at",
        "message_pending_sessions",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_message_pending_sessions_created_at", table_name="message_pending_sessions")
    op.drop_column("message_pending_sessions", "retention_discard_requested_at")
    op.drop_column("message_pending_sessions", "retention_kept_at")
