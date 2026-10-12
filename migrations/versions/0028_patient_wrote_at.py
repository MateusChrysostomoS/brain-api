"""message_patients.patient_wrote_at + activity_tracked: "the patient typed something"

Revision ID: 0028_patient_wrote_at
Revises: 0027_pending_retention_kept
Create Date: 2026-10-09 00:00:00.000000

TASK-042 follow-up (owner, 2026-10-09): the visit-retention job may clean PreCheck clinics too,
as long as the patient never typed anything. brain-api relays every patient message to both
products, so it stamps `patient_wrote_at` itself; PreCheck's automatic greeting never does.

`activity_tracked` separates the rows that can be trusted: every EXISTING row gets False (it
may have written to PreCheck before the stamp existed), and new identities are inserted True by
the ORM. Purely additive; the previous brain-api ignores both columns (its inserts fall back to
the server default False, which is the safe side). Run BEFORE the brain-api that maps them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028_patient_wrote_at"
down_revision: str | None = "0027_pending_retention_kept"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "message_patients",
        sa.Column("patient_wrote_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "message_patients",
        sa.Column(
            "activity_tracked", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )


def downgrade() -> None:
    op.drop_column("message_patients", "activity_tracked")
    op.drop_column("message_patients", "patient_wrote_at")
