"""CourtesyCoupon — cupom que libera acesso sem passar pelo Stripe.

Existe para "family and friends": quem resgata um cupom válido tem a clínica
ativada na hora, sem cartão, sem assinatura e sem cobrança. Deliberadamente NÃO
é um cupom do Stripe de 100% off: aquele caminho ainda exige cartão (o Checkout
do signup é `mode=setup`, que captura cartão antes de qualquer desconto) e
deixaria uma assinatura viva que volta a cobrar sozinha quando o desconto vence.
Aqui não há nada no Stripe para expirar, cobrar ou cancelar.

O resgate reusa `services.signup.provision_tenant_from_intent` sem ids do Stripe
— o caminho que aquela função já documenta como "no-Stripe-ids case" e resolve
para `status="active"`. Ou seja: cortesia não é um segundo caminho de ativação,
é o mesmo com o pagamento pulado.

Como isso dá acesso pago de graça, o portão são os três campos de controle:
`is_active` (desliga na hora), `expires_at` (janela) e `max_uses`/`uses`
(quantas clínicas). `uses` é incrementado sob lock de linha no resgate, senão
dois cliques simultâneos no último uso passariam os dois.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class CourtesyCoupon(Base):
    """Um cupom de cortesia e seus limites de uso."""

    __tablename__ = "courtesy_coupons"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Guardado em MAIÚSCULAS e comparado assim: quem digita "cortesia100" no
    # celular (com autocorreção capitalizando) tem de resgatar o mesmo cupom.
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # Plano que o cupom concede. Um id do catálogo (catalog.PLAN_*), materializado
    # em catalog_ids no resgate — assim a cortesia entrega exatamente o mesmo
    # entitlement que a compra daquele plano entregaria.
    plan_id: Mapped[str] = mapped_column(String(64))

    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), default=True)
    # NULL = sem prazo.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL = ilimitado. Com valor, `uses` não pode alcançá-lo.
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uses: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)

    # Para quem é / por quê — aparece só no admin, nunca para quem resgata.
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
