"""courtesy coupons: acesso liberado sem passar pelo Stripe

Revision ID: 0016_courtesy_coupons
Revises: 0015_precheck_provisioning
Create Date: 2026-08-25 00:00:00.000000

Até aqui só existiam dois jeitos de uma clínica nascer ativa: pagar no Stripe, ou
alguém aprovar à mão um pedido de ativação do lado do PreCheck (o caminho que a
vitrine acabou de perder — ele deixava qualquer visitante entrar sem pagar).

Esta revisão cria o terceiro, controlado: um cupom que ativa a clínica na hora,
sem cartão e sem assinatura. Não é um cupom do Stripe de 100% off, e a diferença
importa: o Checkout do signup é `mode=setup` e captura cartão ANTES de qualquer
desconto, então aquele caminho ainda pediria cartão a quem não vai pagar nada — e
deixaria uma assinatura viva, que volta a cobrar sozinha quando o desconto vence.

`courtesy_coupon_code` no intent é a trilha de auditoria: sem ela, um tenant ativo
sem `stripe_subscription_id` seria indistinguível de uma falha de cobrança.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_courtesy_coupons"
down_revision: Union[str, None] = "0015_precheck_provisioning"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "courtesy_coupons",
        sa.Column("id", sa.Uuid(), primary_key=True),
        # Guardado em maiúsculas; o resgate normaliza antes de comparar.
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("plan_id", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # NULL = ilimitado.
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("uses", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # Único: é a chave que o resgate procura, e dois cupons com o mesmo código
    # tornariam ambíguo qual limite de uso vale.
    op.create_index(
        "ix_courtesy_coupons_code", "courtesy_coupons", ["code"], unique=True
    )

    op.add_column(
        "signup_intents",
        sa.Column("courtesy_coupon_code", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("signup_intents", "courtesy_coupon_code")
    op.drop_index("ix_courtesy_coupons_code", table_name="courtesy_coupons")
    op.drop_table("courtesy_coupons")
