"""patient access: identidade, OTP por e-mail, sessão e consentimento (Brain-Message)

Revision ID: 0018_patient_access
Revises: 0017_message_channels
Create Date: 2026-09-08 00:00:00.000000

A 0017 respondeu "esta clínica atende por qual canal?". Esta responde a pergunta do
outro lado do balcão: "quem é o paciente que chegou por aqui, e como ele prova isso?".

Quatro tabelas, e nenhuma delas é `users`. O paciente NÃO é um usuário da plataforma:
não tem papel, não tem senha e não tem portal. Enfiá-lo em `users` colocaria uma linha
sem papel a uma string de distância de todo gate de `api/deps.py` — a separação aqui é
estrutural, não uma revisão de código bem-feita.

O código do OTP nunca encosta no banco em texto puro: só o SHA-256 vai para
`code_hash`, a mesma disciplina de `users.reset_token_hash` e `refresh_tokens.token_hash`.
O e-mail vai em claro porque é o identificador de login, igual a `users.email` — não é
credencial.

`message_patients.id` é o handle que a secretarIA recebe como `external_id` e o PreCheck
como `session_ref`. UUID canônico de 36 chars: cabe nos dois (64 e 120) com folga, e é
por isso que `patient_consent_events.subject_ref` nasce `String(64)` — o mesmo tamanho
que a `consent_events.wa_id` da secretarIA passou a ter, para que os dois rastros de
consentimento sejam legíveis por um processo só.

Puramente aditiva: nenhuma tabela existente é tocada, nada é backfilled, e um deploy que
não use `/patient-access/*` não muda de comportamento.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018_patient_access"
down_revision: str | None = "0017_message_channels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_patients",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "email", name="uq_message_patients_tenant_email"),
    )
    op.create_index("ix_message_patients_tenant_id", "message_patients", ["tenant_id"])

    op.create_table(
        "message_patient_otps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "email", name="uq_message_patient_otps_tenant_email"),
    )
    op.create_index("ix_message_patient_otps_tenant_id", "message_patient_otps", ["tenant_id"])

    op.create_table(
        "message_patient_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "patient_id",
            sa.Uuid(),
            sa.ForeignKey("message_patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_message_patient_sessions_patient_id", "message_patient_sessions", ["patient_id"]
    )
    op.create_index(
        "ix_message_patient_sessions_tenant_id", "message_patient_sessions", ["tenant_id"]
    )
    op.create_index(
        "ix_message_patient_sessions_token_hash",
        "message_patient_sessions",
        ["token_hash"],
        unique=True,
    )

    op.create_table(
        "patient_consent_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subject_ref", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("legal_basis", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_patient_consent_events_tenant_id", "patient_consent_events", ["tenant_id"])
    op.create_index(
        "ix_patient_consent_events_subject_ref", "patient_consent_events", ["subject_ref"]
    )


def downgrade() -> None:
    # Ordem inversa da criação: sessões antes de pacientes (FK), consentimento por último
    # — ele não tem FK para paciente de propósito (o rastro sobrevive à identidade).
    op.drop_table("patient_consent_events")
    op.drop_table("message_patient_sessions")
    op.drop_table("message_patient_otps")
    op.drop_table("message_patients")
