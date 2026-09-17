"""estado visit-local do OTP inline da sessão pendente

Revision ID: 0022_pending_identity_inline_otp
Revises: 0021_patient_pending_sessions
Create Date: 2026-09-17 00:00:00.000000

`message_patient_account_otps` é corretamente chaveada pelo endereço: o código prova a
CONTA, não uma clínica. Para o chat, porém, o frontend precisa saber se ESTA visita já chegou
ao passo pós-agendamento sem inferir isso de um código solicitado em outra aba/login para o
mesmo e-mail. `otp_requested_at` é esse marcador visit-local.

Migração aditiva e nullable: pode subir antes do código e o brain-api anterior a ignora.
Ordem de deploy: 0022 -> brain-api -> secretarIA -> frontend da onda 3.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_pending_identity_inline_otp"
down_revision: str | None = "0021_patient_pending_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "message_pending_sessions",
        sa.Column("otp_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("message_pending_sessions", "otp_requested_at")
