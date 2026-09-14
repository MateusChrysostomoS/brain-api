"""patient session: renovação in-place (valor anterior + carimbo da rotação)

Revision ID: 0019_patient_session_rotation
Revises: 0018_patient_access
Create Date: 2026-09-14 00:00:00.000000

A 0018 criou a sessão do paciente como irmã de `refresh_tokens`, mas sem a rota que a
renova — `POST /patient-access/refresh` chega agora, e ela NÃO pode renovar do jeito que
o staff renova. `rotate_refresh_token` insere uma linha nova e revoga a antiga a cada uso;
aqui o `id` da linha de login é o `login_sid` de todo token de clínica-irmã vinculada, e
uma linha nova por refresh derrubaria o paciente de todas as outras clínicas abertas.

Então a linha é renovada NO LUGAR: só o valor opaco gira. As duas colunas abaixo guardam
o hash que a última rotação substituiu e quando — o valor anterior continua válido por
`PATIENT_SESSION_ROTATION_GRACE_SECONDS`, para que duas renovações em voo com o mesmo
cookie (o portal faz polling de várias threads e clínicas ao mesmo tempo) não sejam lidas
como roubo. Apresentar o valor anterior DEPOIS da janela é o sinal de reuso, e revoga a
conta inteira — a mesma semântica do staff, em outra forma.

Puramente aditiva e anulável: nenhuma linha existente muda, nenhum backfill, e uma sessão
aberta antes desta migração renova normalmente na primeira chamada.

Aplicar em produção ANTES de deployar o brain-api que expõe `/patient-access/refresh`
(`alembic upgrade head` no console do EasyPanel, como a 0018) — sem ela a rota 500a.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019_patient_session_rotation"
down_revision: str | None = "0018_patient_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "message_patient_sessions",
        sa.Column("previous_token_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "message_patient_sessions",
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_message_patient_sessions_previous_token_hash",
        "message_patient_sessions",
        ["previous_token_hash"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_message_patient_sessions_previous_token_hash",
        table_name="message_patient_sessions",
    )
    op.drop_column("message_patient_sessions", "rotated_at")
    op.drop_column("message_patient_sessions", "previous_token_hash")
