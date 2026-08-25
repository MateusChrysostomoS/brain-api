"""Cria (ou atualiza) um cupom de cortesia — acesso liberado sem passar pelo Stripe.

Quem resgata um cupom válido no cadastro tem a clínica ativada na hora, sem
cartão e sem assinatura. Não existe nada no Stripe para esse tenant: nada para
expirar, cobrar ou cancelar depois.

Como isso dá acesso pago de graça, prefira sempre criar com limite:
`--max-uses` fecha o cupom depois de N clínicas e `--expires-in-days` fecha por
prazo. Sem nenhum dos dois o cupom é ilimitado e vale para sempre — e um código
vazado num grupo de WhatsApp vira um produto grátis.

Idempotente: rodar de novo com o mesmo `--code` ATUALIZA os limites do cupom
existente e preserva o contador de usos (para não zerar o controle sem querer).
`--deactivate` desliga um cupom na hora, sem apagar o histórico.

Uso:
    uv run python scripts/create_courtesy_coupon.py \
        --code CORTESIA100 --plan precheck_basic --max-uses 10

    # desligar
    uv run python scripts/create_courtesy_coupon.py --code CORTESIA100 --deactivate

    # ver o estado atual
    uv run python scripts/create_courtesy_coupon.py --code CORTESIA100 --show
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from brain_api.core.database import async_session_factory
from brain_api.core.logging import get_logger, setup_logging
from brain_api.models import CourtesyCoupon
from brain_api.services import courtesy, catalog

logger = get_logger(__name__)


def _descreve(c: CourtesyCoupon) -> str:
    usos = f"{c.uses}/{c.max_uses}" if c.max_uses is not None else f"{c.uses} (ilimitado)"
    prazo = c.expires_at.isoformat() if c.expires_at else "sem prazo"
    estado = "ATIVO" if c.is_active else "desativado"
    return f"{c.code}  {estado}  plano={c.plan_id}  usos={usos}  expira={prazo}"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", required=True, help="Código do cupom (case-insensitive)")
    parser.add_argument("--plan", default="precheck_basic", help="Plano concedido")
    parser.add_argument("--max-uses", type=int, default=None, help="Limite de resgates")
    parser.add_argument("--expires-in-days", type=int, default=None, help="Validade em dias")
    parser.add_argument("--note", default=None, help="Para quem é / por quê (só admin)")
    parser.add_argument("--deactivate", action="store_true", help="Desliga o cupom")
    parser.add_argument("--show", action="store_true", help="Só mostra o estado atual")
    args = parser.parse_args()

    setup_logging()
    code = courtesy.normalize(args.code)

    async with async_session_factory() as session:
        existente = await session.scalar(
            select(CourtesyCoupon).where(CourtesyCoupon.code == code)
        )

        if args.show:
            print(_descreve(existente) if existente else f"{code}: não existe")
            return 0

        if args.deactivate:
            if existente is None:
                print(f"{code}: não existe", file=sys.stderr)
                return 1
            existente.is_active = False
            await session.commit()
            print(f"desativado: {_descreve(existente)}")
            return 0

        if not courtesy.plano_valido(args.plan):
            validos = ", ".join(sorted(catalog.PLAN_IDS))
            print(f"plano desconhecido: {args.plan}\nplanos: {validos}", file=sys.stderr)
            return 1

        expira = (
            datetime.now(UTC) + timedelta(days=args.expires_in_days)
            if args.expires_in_days is not None
            else None
        )

        if existente is None:
            session.add(
                CourtesyCoupon(
                    code=code,
                    plan_id=args.plan,
                    max_uses=args.max_uses,
                    expires_at=expira,
                    note=args.note,
                    is_active=True,
                )
            )
            await session.commit()
            criado = await session.scalar(
                select(CourtesyCoupon).where(CourtesyCoupon.code == code)
            )
            print(f"criado: {_descreve(criado)}")
        else:
            # `uses` NÃO é tocado: reeditar os limites não pode zerar o controle
            # de quantas clínicas o cupom já liberou.
            existente.plan_id = args.plan
            existente.max_uses = args.max_uses
            existente.expires_at = expira
            existente.is_active = True
            if args.note is not None:
                existente.note = args.note
            await session.commit()
            print(f"atualizado: {_descreve(existente)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
