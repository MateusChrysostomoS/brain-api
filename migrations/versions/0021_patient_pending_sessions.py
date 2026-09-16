"""sessão pendente: o paciente conversa (e marca consulta) antes de provar o e-mail

Revision ID: 0021_patient_pending_sessions
Revises: 0020_patient_accounts
Create Date: 2026-09-16 00:00:00.000000

Até a 0020 nenhuma identidade de paciente nascia sem um código verificado: `verify-otp` era o
único lugar que criava `MessagePatient`. Decisão do dono (2026-09-16): um paciente NOVO entra
pelo link da clínica, cai direto no chat, dá o e-mail DURANTE a conversa, marca a consulta de
verdade, e só depois recebe o código. Isso precisa de algo que carregue a conversa antes de
existir conta.

O que esta migração cria:

- `message_pending_sessions`: a visita em si (identidade apontada, clínica, e-mail REIVINDICADO
  mas não provado, token opaco por hash, expiração, revogação). Tabela própria, e não uma linha
  de `message_patient_sessions` com `account_id` nulo, porque AQUELA forma já significa outra
  coisa (login de antes do modelo de conta, que adota a própria identidade quando o cookie
  renova — `services/patient_access.py::_adopt`). Duas coisas diferentes na mesma tabela viraria
  uma distinção que só existe na cabeça de quem lê três colunas anuláveis na ordem certa.
- `message_patients.email` vira ANULÁVEL: a identidade (o handle que a secretarIA e o PreCheck
  veem) nasce junto com a visita, sem endereço. O endereço entra uma única vez, quando um código
  o prova. A unique `(tenant_id, email)` continua valendo para endereços reais — Postgres e
  SQLite tratam NULLs como distintos, então uma clínica pode ter muitas linhas pendentes ao
  mesmo tempo e no máximo uma identidade por endereço.

NENHUM `MessagePatient.id` MUDA — nem aqui, nem no código novo. É o `external_id` da secretarIA
e o `session_ref` do PreCheck, e a conversa inteira (inclusive o agendamento feito antes do
código) vive embaixo dele.

ORDEM DE DEPLOY: `alembic upgrade head` → brain-api → só então secretarIA (que passa a chamar
`POST /internal/brain-message/pending-email`) e o frontend novo. A migração só ALARGA
(`NOT NULL` → anulável, tabela nova): o brain-api anterior continua funcionando sobre este
schema sem enxergar nada disso (frozen-contract-migration).

DOWNGRADE: pare o brain-api novo antes. Apaga as identidades ainda pendentes (`email IS NULL`),
que o código antigo não sabe ler, junto com as sessões pendentes; identidades já adotadas (com
endereço) ficam intactas, com os mesmos ids. Uma conversa pendente em andamento se perde — é o
preço de voltar para um schema onde ela não pode existir.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021_patient_pending_sessions"
down_revision: str | None = "0020_patient_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "message_patients", "email", existing_type=sa.String(length=255), nullable=True
    )

    op.create_table(
        "message_pending_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["patient_id"], ["message_patients.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["superseded_by"], ["message_patients.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_message_pending_sessions_token_hash"),
    )
    op.create_index(
        "ix_message_pending_sessions_patient_id", "message_pending_sessions", ["patient_id"]
    )
    op.create_index(
        "ix_message_pending_sessions_tenant_id", "message_pending_sessions", ["tenant_id"]
    )
    op.create_index(
        "ix_message_pending_sessions_token_hash", "message_pending_sessions", ["token_hash"]
    )


def downgrade() -> None:
    op.drop_index("ix_message_pending_sessions_token_hash", table_name="message_pending_sessions")
    op.drop_index("ix_message_pending_sessions_tenant_id", table_name="message_pending_sessions")
    op.drop_index("ix_message_pending_sessions_patient_id", table_name="message_pending_sessions")
    op.drop_table("message_pending_sessions")

    # The identities that never got an address cannot survive a NOT NULL `email`, and the old
    # code could not read them anyway. Their consent trail goes with them: a pending identity
    # has none (the `brain_message_channel_access` event is recorded when the clinic enters an
    # account, which is exactly what never happened here). Delete explicitly rather than by FK
    # cascade so the order — and the fact that something IS deleted — is readable.
    op.execute(
        "DELETE FROM patient_consent_events WHERE subject_ref IN ("
        "SELECT CAST(id AS VARCHAR) FROM message_patients WHERE email IS NULL)"
    )
    op.execute("DELETE FROM message_patients WHERE email IS NULL")
    op.alter_column(
        "message_patients", "email", existing_type=sa.String(length=255), nullable=False
    )
