"""patient accounts: login só por e-mail + clínica entra na conta por convite

Revision ID: 0020_patient_accounts
Revises: 0019_patient_session_rotation
Create Date: 2026-09-15 00:00:00.000000

Até a 0019 o login do paciente era POR CLÍNICA: o código era emitido para um par
`(tenant_id, email)` e a sessão apontava para um `MessagePatient`. Decisão do dono
(2026-09-14): e-mail + código abrem a CONTA, e uma clínica só entra na conta por um gesto da
paciente (link da clínica, ou link/código curto colado). O que esta migração cria:

- `message_patient_accounts` (e-mail único) e `message_patient_account_otps` (o desafio por
  e-mail, sem clínica). A antiga `message_patient_otps` fica intocada: o brain-api anterior
  ainda escreve nela durante a janela de deploy; sai numa migração futura.
- `message_patients.account_id` (FK SET NULL) + índice em `message_patients.email`.
- `message_patient_sessions.account_id` (FK CASCADE); `patient_id`/`tenant_id` passam a
  aceitar NULL (login aberto só por e-mail não tem clínica).
- `tenants.patient_invite_code` (8 caracteres sem ambíguos, único, anulável — um tenant criado
  pelo brain-api anterior na janela recebe o código na primeira leitura do staff).

BACKFILL — nenhum `MessagePatient.id` muda (é o `external_id` da secretarIA e o
`session_ref` do PreCheck). Aqui entra numa conta SÓ a identidade com vínculo confirmado
(`brain_message_account_link`): essa clínica já voltava em todo login do endereço. A
identidade de um login antigo entra quando o cookie DAQUELE login renovar
(`services/patient_access.py::_adopt`), e assim cada navegador reabre exatamente as clínicas
de antes. O consentimento `brain_message_channel_access` NÃO serve de critério: toda
identidade antiga tem um (gravado junto com cada código), então ele juntaria na conta
clínicas que a paciente só viu como pergunta — ou recusou, sem registro. As sessões das
identidades adotadas herdam o `account_id` (mesmo `id` de linha: cookies e tokens emitidos
antes seguem valendo).

SÓ POSTGRES: o casamento `subject_ref = CAST(id AS VARCHAR)` depende do texto com hífens do
UUID do Postgres. Em outro dialeto a migração se recusa a rodar, em vez de não adotar ninguém
em silêncio.

DOWNGRADE — pare o brain-api novo antes (ele grava linhas de login sem clínica, que o
`SET NOT NULL` recusaria). Apaga as linhas de login só-de-conta (quem as usava faz login de
novo) e DESCARTA os códigos de convite: um upgrade seguinte cunha códigos NOVOS, e os links e
códigos que as clínicas já entregaram deixam de funcionar. Identidades, consentimentos e as
demais sessões ficam.

Ordem: `alembic upgrade head` → deploy do brain-api → só então o frontend novo.
"""

import secrets
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020_patient_accounts"
down_revision: str | None = "0019_patient_session_rotation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A frozen copy of `core/invite_codes.py`'s format: a migration must not change meaning when
# the app module does.
_INVITE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_INVITE_LENGTH = 8

# The adoption rule, per identity `mp`: a link the patient confirmed by name.
_ADOPTED = """
EXISTS (
    SELECT 1 FROM patient_consent_events e
    WHERE e.tenant_id = mp.tenant_id
      AND e.subject_ref = CAST(mp.id AS VARCHAR)
      AND e.kind = 'brain_message_account_link'
)
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("0020_patient_accounts runs on Postgres only (see the docstring)")

    op.create_table(
        "message_patient_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_message_patient_accounts_email"),
    )
    op.create_table(
        "message_patient_account_otps",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_message_patient_account_otps_email"),
    )

    op.add_column("message_patients", sa.Column("account_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_message_patients_account_id",
        "message_patients",
        "message_patient_accounts",
        ["account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_message_patients_account_id", "message_patients", ["account_id"])
    op.create_index("ix_message_patients_email", "message_patients", ["email"])

    op.add_column("message_patient_sessions", sa.Column("account_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_message_patient_sessions_account_id",
        "message_patient_sessions",
        "message_patient_accounts",
        ["account_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_message_patient_sessions_account_id", "message_patient_sessions", ["account_id"]
    )
    op.alter_column(
        "message_patient_sessions", "patient_id", existing_type=sa.Uuid(), nullable=True
    )
    op.alter_column(
        "message_patient_sessions", "tenant_id", existing_type=sa.Uuid(), nullable=True
    )

    op.add_column(
        "tenants", sa.Column("patient_invite_code", sa.String(length=16), nullable=True)
    )
    op.create_unique_constraint(
        "uq_tenants_patient_invite_code", "tenants", ["patient_invite_code"]
    )

    _backfill_invite_codes(bind)
    _backfill_accounts(bind)


def _backfill_invite_codes(bind: sa.Connection) -> None:
    taken = {
        code
        for (code,) in bind.execute(
            sa.text("SELECT patient_invite_code FROM tenants WHERE patient_invite_code IS NOT NULL")
        )
    }
    missing = [
        tenant_id
        for (tenant_id,) in bind.execute(
            sa.text("SELECT id FROM tenants WHERE patient_invite_code IS NULL")
        )
    ]
    for tenant_id in missing:
        code = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(_INVITE_LENGTH))
        while code in taken:
            code = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(_INVITE_LENGTH))
        taken.add(code)
        bind.execute(
            sa.text("UPDATE tenants SET patient_invite_code = :code WHERE id = :id"),
            {"code": code, "id": tenant_id},
        )


def _backfill_accounts(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT mp.email, min(mp.created_at), max(mp.last_seen_at) "
            f"FROM message_patients mp WHERE {_ADOPTED} GROUP BY mp.email"
        )
    ).all()
    if rows:
        accounts = sa.table(
            "message_patient_accounts",
            sa.column("id", sa.Uuid()),
            sa.column("email", sa.String()),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("last_seen_at", sa.DateTime(timezone=True)),
        )
        op.bulk_insert(
            accounts,
            [
                {
                    "id": uuid.uuid4(),
                    "email": email,
                    "created_at": created_at or datetime.now(UTC),
                    "last_seen_at": last_seen_at,
                }
                for email, created_at, last_seen_at in rows
            ],
        )
    bind.execute(
        sa.text(
            "UPDATE message_patients mp SET account_id = a.id "
            "FROM message_patient_accounts a "
            f"WHERE a.email = mp.email AND mp.account_id IS NULL AND {_ADOPTED}"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE message_patient_sessions s SET account_id = mp.account_id "
            "FROM message_patients mp "
            "WHERE s.patient_id = mp.id AND s.account_id IS NULL AND mp.account_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM message_patient_sessions WHERE patient_id IS NULL OR tenant_id IS NULL"
    )
    op.drop_constraint("uq_tenants_patient_invite_code", "tenants", type_="unique")
    op.drop_column("tenants", "patient_invite_code")

    op.alter_column(
        "message_patient_sessions", "tenant_id", existing_type=sa.Uuid(), nullable=False
    )
    op.alter_column(
        "message_patient_sessions", "patient_id", existing_type=sa.Uuid(), nullable=False
    )
    op.drop_index("ix_message_patient_sessions_account_id", table_name="message_patient_sessions")
    op.drop_constraint(
        "fk_message_patient_sessions_account_id", "message_patient_sessions", type_="foreignkey"
    )
    op.drop_column("message_patient_sessions", "account_id")

    op.drop_index("ix_message_patients_email", table_name="message_patients")
    op.drop_index("ix_message_patients_account_id", table_name="message_patients")
    op.drop_constraint("fk_message_patients_account_id", "message_patients", type_="foreignkey")
    op.drop_column("message_patients", "account_id")

    op.drop_table("message_patient_account_otps")
    op.drop_table("message_patient_accounts")
