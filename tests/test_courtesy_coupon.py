"""Tests do cupom de cortesia — a clínica que nasce ativa sem passar pelo Stripe.

O que está do outro lado deste endpoint é acesso pago de graça, então a suíte
gasta mais tempo nas RECUSAS do que no caminho feliz:

- cupom inexistente, desativado, expirado e esgotado — todos com a MESMA
  mensagem, porque distingui-los ensinaria um atacante quais códigos existem;
- resgate duplo do mesmo intent (daria uma segunda clínica de graça);
- corrida no último uso disponível;
- o plano vem do CUPOM, não do que o visitante selecionou (senão bastaria
  escolher o plano caro antes de resgatar um cupom do básico).

E o que o resgate tem de entregar igual ao caminho pago: entitlement ativo, e o
mesmo token de onboarding que o polling emitiria — é por ele que o médico entra.

Ground truth: services/courtesy.py, api/public_signup.py
(`POST /public/courtesy-redemptions`), e o reuso de
`signup.provision_tenant_from_intent` sem ids do Stripe.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from brain_api.models import CourtesyCoupon, Entitlement, SignupIntent
from brain_api.services import courtesy as courtesy_service

SIGNUP_PASSWORD = "signup123"


@asynccontextmanager
async def _sessao():
    """Sessão sobre o MESMO banco que o `client` usa.

    A fixture `db_session` da conftest abre um engine PRÓPRIO — um cupom criado
    por ela é invisível para a rota, e o resgate recusa tudo. Este é o padrão
    que tests/test_signup.py já usa para espiar o estado por trás do client.
    """
    from brain_api.core.database import get_session
    from brain_api.main import app

    gen = app.dependency_overrides[get_session]()
    session = await gen.__anext__()
    try:
        yield session
    finally:
        await gen.aclose()


def _register_body(**overrides) -> dict:
    body = {
        "name": "Dr. Amigo",
        "clinic_name": "Clinica do Amigo",
        "email": "amigo@example.com",
        "whatsapp_phone": "+5511988887777",
        "password": SIGNUP_PASSWORD,
        "catalog_ids": ["precheck_basic"],
    }
    body.update(overrides)
    return body


async def _cupom(**overrides) -> None:
    dados = {
        "code": "CORTESIA100",
        "plan_id": "precheck_basic",
        "is_active": True,
        "expires_at": None,
        "max_uses": None,
        "uses": 0,
    }
    dados.update(overrides)
    async with _sessao() as session:
        session.add(CourtesyCoupon(**dados))
        await session.commit()


async def _le_cupom(code: str = "CORTESIA100") -> CourtesyCoupon:
    from sqlalchemy import select

    async with _sessao() as session:
        return await session.scalar(
            select(CourtesyCoupon).where(CourtesyCoupon.code == code)
        )


async def _le_intent(intent_id: str) -> SignupIntent:
    async with _sessao() as session:
        return await session.get(SignupIntent, UUID(intent_id))


async def _le_entitlement(tenant_id) -> Entitlement:
    async with _sessao() as session:
        return await session.get(Entitlement, tenant_id)


async def _registra(client, **overrides) -> str:
    resp = await client.post("/public/signup-intents", json=_register_body(**overrides))
    assert resp.status_code == 201, resp.text
    return resp.json()["intent_id"]


# ── caminho feliz ───────────────────────────────────────────────────────────


async def test_resgate_ativa_a_clinica_sem_stripe(client):
    await _cupom()
    intent_id = await _registra(client)

    resp = await client.post(
        "/public/courtesy-redemptions",
        json={"intent_id": intent_id, "code": "CORTESIA100"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["status"] == "ready"
    assert body["products"]["precheck"] is True
    # É por este token que o médico entra — sem ele o resgate não serviria de nada
    assert body["onboarding_token"]

    intent = await _le_intent(intent_id)
    assert intent.status == "completed"
    assert intent.courtesy_coupon_code == "CORTESIA100"
    # o ponto do cupom: nada no Stripe para cobrar, expirar ou cancelar
    assert intent.stripe_customer_id is None
    assert intent.stripe_subscription_id is None

    ent = await _le_entitlement(intent.tenant_id)
    assert ent.status == "active"
    assert ent.precheck_enabled is True
    assert ent.stripe_subscription_id is None


async def test_codigo_normalizado_no_resgate(client):
    """Quem digita no celular recebe capitalização da autocorreção e às vezes um
    espaço colado do copiar/colar. Nenhum dos dois pode virar outro cupom."""
    await _cupom()
    intent_id = await _registra(client)

    resp = await client.post(
        "/public/courtesy-redemptions",
        json={"intent_id": intent_id, "code": "  cortesia100 "},
    )
    assert resp.status_code == 200, resp.text


async def test_plano_vem_do_cupom_e_nao_da_escolha(client):
    """Senão bastaria selecionar o plano mais caro antes de resgatar um cupom do
    básico."""
    await _cupom(plan_id="precheck_basic")
    intent_id = await _registra(client, catalog_ids=["precheck_advanced"])

    resp = await client.post(
        "/public/courtesy-redemptions",
        json={"intent_id": intent_id, "code": "CORTESIA100"},
    )
    assert resp.status_code == 200, resp.text

    intent = await _le_intent(intent_id)
    assert intent.catalog_ids == ["precheck_basic"]
    ent = await _le_entitlement(intent.tenant_id)
    assert ent.plan == "precheck_basic"


async def test_conta_o_uso(client):
    await _cupom(max_uses=3)
    intent_id = await _registra(client)

    await client.post(
        "/public/courtesy-redemptions",
        json={"intent_id": intent_id, "code": "CORTESIA100"},
    )
    coupon = await _le_cupom()
    assert coupon.uses == 1


# ── recusas: todas com a mesma resposta ─────────────────────────────────────


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({}, "NAOEXISTE"),                                             # inexistente
        ({"is_active": False}, "CORTESIA100"),                         # desativado
        ({"expires_at": datetime.now(UTC) - timedelta(days=1)}, "CORTESIA100"),  # expirado
        ({"max_uses": 2, "uses": 2}, "CORTESIA100"),                   # esgotado
    ],
    ids=["inexistente", "desativado", "expirado", "esgotado"],
)
async def test_cupom_recusado_nao_ativa_nada(client, overrides, code):
    await _cupom(**overrides)
    intent_id = await _registra(client)

    resp = await client.post(
        "/public/courtesy-redemptions",
        json={"intent_id": intent_id, "code": code},
    )
    assert resp.status_code == 422
    # mesma mensagem para os quatro motivos: um "expirado" distinto de um
    # "inexistente" mapeia quais códigos existem
    assert resp.json()["detail"] == "coupon_invalid"

    intent = await _le_intent(intent_id)
    assert intent.status == "pending_payment"
    ent = await _le_entitlement(intent.tenant_id)
    assert ent.status == "inactive"


async def test_recusa_nao_gasta_uso(client):
    await _cupom(is_active=False, max_uses=5)
    intent_id = await _registra(client)

    await client.post(
        "/public/courtesy-redemptions",
        json={"intent_id": intent_id, "code": "CORTESIA100"},
    )
    coupon = await _le_cupom()
    assert coupon.uses == 0


async def test_expira_na_hora_exata(client):
    """`expires_at` no passado por um segundo já recusa — a comparação é <=, não <."""
    await _cupom(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    intent_id = await _registra(client)
    resp = await client.post(
        "/public/courtesy-redemptions",
        json={"intent_id": intent_id, "code": "CORTESIA100"},
    )
    assert resp.status_code == 422


async def test_ainda_valido_antes_de_expirar(client):
    await _cupom(expires_at=datetime.now(UTC) + timedelta(days=1))
    intent_id = await _registra(client)
    resp = await client.post(
        "/public/courtesy-redemptions",
        json={"intent_id": intent_id, "code": "CORTESIA100"},
    )
    assert resp.status_code == 200, resp.text


# ── o intent só pode ser resgatado uma vez ──────────────────────────────────


async def test_resgate_duplo_recusado(client):
    """A segunda chamada daria uma segunda clínica de graça pelo mesmo cadastro."""
    await _cupom(max_uses=10)
    intent_id = await _registra(client)

    primeira = await client.post(
        "/public/courtesy-redemptions",
        json={"intent_id": intent_id, "code": "CORTESIA100"},
    )
    assert primeira.status_code == 200

    segunda = await client.post(
        "/public/courtesy-redemptions",
        json={"intent_id": intent_id, "code": "CORTESIA100"},
    )
    assert segunda.status_code == 409
    coupon = await _le_cupom()
    assert coupon.uses == 1          # o segundo não gastou uso


async def test_intent_desconhecido(client):
    await _cupom()
    resp = await client.post(
        "/public/courtesy-redemptions",
        json={
            "intent_id": "00000000-0000-4000-8000-000000000000",
            "code": "CORTESIA100",
        },
    )
    assert resp.status_code == 404


# ── limite de usos como portão real ─────────────────────────────────────────


async def test_ultimo_uso_fecha_o_cupom(client):
    await _cupom(max_uses=1)
    primeiro = await _registra(client, email="um@example.com")
    segundo = await _registra(client, email="dois@example.com")

    ok = await client.post(
        "/public/courtesy-redemptions",
        json={"intent_id": primeiro, "code": "CORTESIA100"},
    )
    assert ok.status_code == 200

    depois = await client.post(
        "/public/courtesy-redemptions",
        json={"intent_id": segundo, "code": "CORTESIA100"},
    )
    assert depois.status_code == 422

    coupon = await _le_cupom()
    assert coupon.uses == 1

    # o segundo cadastro continua intacto e pode pagar normalmente
    intent = await _le_intent(segundo)
    assert intent.status == "pending_payment"


async def test_max_uses_nulo_e_ilimitado(client):
    await _cupom(max_uses=None)
    for i in range(3):
        intent_id = await _registra(client, email=f"ilimitado{i}@example.com")
        resp = await client.post(
            "/public/courtesy-redemptions",
            json={"intent_id": intent_id, "code": "CORTESIA100"},
        )
        assert resp.status_code == 200, resp.text
    coupon = await _le_cupom()
    assert coupon.uses == 3


# ── normalização ────────────────────────────────────────────────────────────


def test_normalize():
    assert courtesy_service.normalize("  cortesia100 ") == "CORTESIA100"
    assert courtesy_service.normalize("CoRtEsIa100") == "CORTESIA100"
    assert courtesy_service.normalize("CORTESIA100") == "CORTESIA100"
