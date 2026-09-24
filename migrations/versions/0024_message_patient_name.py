"""message_patients: the name the patient gave, so the next clinic does not ask again

Revision ID: 0024_message_patient_name
Revises: 0023_brain_message_default_on
Create Date: 2026-09-24 00:00:00.000000

A Portal ACCOUNT added to a new clinic now skips e-mail and code
(`docs/CHECKPOINT_portal_sessao_ativa_pula_pendente.md`), but the clinic still needs the
patient's name — the calendar event's title, the professional's e-mail, the PreCheck hand-off
(owner, 2026-09-24). secretarIA asks it once and reports it back
(`POST /internal/brain-message/patient-name`); brain-api keeps it on the clinic identity and
sends it on the `open` of the account's next clinic.

Purely additive, both columns nullable, no backfill: a NULL name only means "ask once".
Must run BEFORE the brain-api that maps the columns (the ORM selects them on every
`message_patients` read).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024_message_patient_name"
down_revision: str | None = "0023_brain_message_default_on"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("message_patients", sa.Column("name", sa.String(length=255), nullable=True))
    op.add_column(
        "message_patients",
        sa.Column("name_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("message_patients", "name_updated_at")
    op.drop_column("message_patients", "name")
