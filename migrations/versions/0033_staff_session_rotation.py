"""sessão pessoal de equipe: renovação in-place (valor anterior + carimbo da rotação)

Revision ID: 0033_staff_session_rotation
Revises: 0032_staff_membership_email
Create Date: 2026-10-20 00:00:00.000000

TASK-065. `POST /auth/account/resume` revogava a linha de `staff_account_sessions` e abria
outra a cada renovação. O `id` dessa linha é o `account_sid` de todo token clínico emitido
por `/auth/account/select`, então cada renovação derrubava os tokens clínicos das outras
abas da mesma pessoa, e duas renovações em voo com o mesmo cookie eram lidas como roubo
(conta inteira encerrada).

A linha passa a ser renovada NO LUGAR, como a sessão do paciente (0019): só o valor opaco
gira. As duas colunas abaixo guardam o hash que a última rotação substituiu e quando; o
valor anterior continua aceito por `STAFF_ACCOUNT_SESSION_ROTATION_GRACE_SECONDS`.
Apresentá-lo DEPOIS da janela continua sendo o sinal de reuso.

Puramente aditiva e anulável: nenhuma linha existente muda, sem backfill, e uma sessão
aberta antes desta migração renova normalmente na primeira chamada.

Aplicar ANTES de deployar o brain-api que a usa (`alembic upgrade head`, que também aplica a
0032 se ela ainda não estiver aplicada) — sem ela, login/resume da conta pessoal 500am.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0033_staff_session_rotation"
down_revision: str | None = "0032_staff_membership_email"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "staff_account_sessions",
        sa.Column("previous_token_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "staff_account_sessions",
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_staff_account_sessions_previous_token_hash",
        "staff_account_sessions",
        ["previous_token_hash"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_staff_account_sessions_previous_token_hash",
        table_name="staff_account_sessions",
    )
    op.drop_column("staff_account_sessions", "rotated_at")
    op.drop_column("staff_account_sessions", "previous_token_hash")
