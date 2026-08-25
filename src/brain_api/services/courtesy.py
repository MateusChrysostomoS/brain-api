"""Resgate de cupom de cortesia — ativa a clínica sem passar pelo Stripe.

O caminho de compra é: registra (tenant + owner + entitlement inerte) -> Checkout
-> webhook -> `signup.provision_tenant_from_intent` ATIVA o entitlement. O resgate
de cortesia entra exatamente no último passo, pulando os dois do meio. Não é uma
segunda implementação de ativação — é a mesma função, chamada sem ids do Stripe,
que ela já documenta como o caso que resolve para `status="active"`.

Consequência que vale registrar: uma clínica de cortesia não tem customer nem
subscription no Stripe. Não há nada para expirar, cobrar ou cancelar — que é
justamente o ponto, e o motivo de isto existir em vez de um cupom de 100% off lá
(aquele ainda exigiria cartão, porque o Checkout do signup é `mode=setup`).

Falha fechada: qualquer motivo de recusa devolve a MESMA mensagem genérica. Um
"cupom expirado" distinto de "cupom inexistente" ensinaria um atacante quais
códigos existem, e o que está do outro lado é acesso pago de graça.
"""

from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.core.logging import get_logger
from brain_api.models import CourtesyCoupon, SignupIntent
from brain_api.services import catalog, signup as signup_service

logger = get_logger(__name__)

# Uma recusa é sempre esta. Ver a nota de falha fechada no topo.
_RECUSA = "coupon_invalid"


def normalize(code: str) -> str:
    """Forma canônica do código: sem espaços das pontas e em maiúsculas.

    Quem digita no celular recebe a primeira letra maiúscula da autocorreção e
    às vezes um espaço colado do copiar/colar. Nenhum dos dois deve virar um
    cupom diferente.
    """
    return code.strip().upper()


async def redeem(session: AsyncSession, intent_id, code: str) -> SignupIntent:
    """Resgata `code` para `intent_id` e devolve o intent já ativado.

    Levanta 404 para intent desconhecido, 409 se o intent já saiu de
    `pending_payment` (pago ou já resgatado — resgatar de novo daria uma segunda
    clínica de graça), e 422 para qualquer recusa do cupom.

    O incremento de `uses` acontece sob lock de linha: sem ele, dois resgates
    simultâneos do último uso disponível leriam `uses` antes de qualquer escrita
    e passariam os dois.
    """
    intent = await session.get(SignupIntent, intent_id)
    if intent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "signup_intent_not_found")
    if intent.status != "pending_payment":
        raise HTTPException(status.HTTP_409_CONFLICT, "intent_not_pending")

    normalizado = normalize(code)
    coupon = await session.scalar(
        select(CourtesyCoupon)
        .where(CourtesyCoupon.code == normalizado)
        .with_for_update()  # segura a linha até o commit — ver docstring
    )

    agora = datetime.now(UTC)
    if (
        coupon is None
        or not coupon.is_active
        # `_as_utc`: o SQLite devolve datetime naive mesmo em coluna timezone=True,
        # e comparar naive com aware levanta TypeError — numa rota pública isso
        # seria um 500 em vez de uma recusa.
        or (
            coupon.expires_at is not None
            and signup_service._as_utc(coupon.expires_at) <= agora
        )
        or (coupon.max_uses is not None and coupon.uses >= coupon.max_uses)
    ):
        # Só o log distingue os motivos; a resposta, nunca.
        logger.info(
            "courtesy_redeem_refused",
            intent_id=str(intent_id),
            found=coupon is not None,
            active=bool(coupon and coupon.is_active),
            expired=bool(
                coupon
                and coupon.expires_at
                and signup_service._as_utc(coupon.expires_at) <= agora
            ),
            exhausted=bool(
                coupon and coupon.max_uses is not None and coupon.uses >= coupon.max_uses
            ),
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, _RECUSA)

    # O cupom decide o plano, não o que o visitante selecionou na vitrine: senão
    # bastaria escolher o plano mais caro antes de resgatar um cupom do básico.
    # Os add-ons que ele escolheu são descartados pelo mesmo motivo.
    intent.catalog_ids = [coupon.plan_id]
    intent.courtesy_coupon_code = coupon.code

    await signup_service.provision_tenant_from_intent(
        session,
        intent,
        stripe_customer_id=None,
        stripe_subscription_id=None,
    )
    if intent.status != "completed":
        # provision_tenant_from_intent falha SEM levantar (marca o intent como
        # "failed") quando o tenant sumiu. No webhook isso é certo — o Stripe
        # precisa do ack. Aqui há alguém esperando na tela: não pode virar sucesso
        # silencioso, e o uso do cupom não pode ser gasto.
        await session.rollback()
        logger.warning("courtesy_redeem_activation_failed", intent_id=str(intent_id))
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            intent.failure_reason or "activation_failed",
        )

    coupon.uses += 1
    await session.commit()
    logger.info(
        "courtesy_redeemed",
        intent_id=str(intent_id),
        code=coupon.code,
        plan=coupon.plan_id,
        uses=coupon.uses,
    )
    await session.refresh(intent)
    return intent


def plano_valido(plan_id: str) -> bool:
    """O plano existe no catálogo? Usado ao cadastrar um cupom."""
    return catalog.get_plan(plan_id) is not None
