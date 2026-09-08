"""tenants: canais de entrega (whatsapp / brain_message)

Revision ID: 0017_message_channels
Revises: 0016_courtesy_coupons
Create Date: 2026-09-08 00:00:00.000000

Até aqui o canal não existia no modelo de dados: todo tenant era WhatsApp por
omissão, porque não havia outro. Com o Brain-Message (console de atendimento +
portal do paciente) isso deixa de ser verdade, e nada no banco sabia responder
"esta clínica atende por qual canal?".

Dois booleanos e não um enum: canal aqui é um CONJUNTO, igual a
`entitlements.precheck_enabled` / `secretaria_enabled`. Uma clínica migra aos
poucos — mantém o WhatsApp dos pacientes antigos e oferece o Brain-Message aos
novos —, e um enum único forçaria um corte tudo-ou-nada que não é o caso de uso.

Canal é propriedade do TENANT, não do plano: `services/catalog.py::PlanDef` fica
intocado de propósito. Trocar de canal não é trocar de plano.

Aditiva e sem mudança de comportamento: os dois nascem `false`. O backfill marca
`whatsapp_enabled = true` só para quem tem `connected_at` carimbado — o único
sinal, dentro deste repo, de que a WABA foi de fato conectada
(`services/onboarding.py::record_attempt` carimba no attempt 'pass'). Um tenant
que nunca chegou a conectar número continua `false`/`false`, que é a verdade
sobre ele.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_message_channels"
down_revision: str | None = "0016_courtesy_coupons"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "whatsapp_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "brain_message_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )

    # Backfill: todo tenant que já conectou um número está, de fato, no canal WhatsApp.
    # Escopo deliberadamente estreito — `connected_at IS NOT NULL` e nada mais: um tenant
    # ainda em onboarding (pendente/aquecimento/bloqueado) não atende por canal nenhum
    # hoje, e marcá-lo aqui seria inventar um estado que ele não tem.
    op.execute("UPDATE tenants SET whatsapp_enabled = true WHERE connected_at IS NOT NULL")


def downgrade() -> None:
    op.drop_column("tenants", "brain_message_enabled")
    op.drop_column("tenants", "whatsapp_enabled")
