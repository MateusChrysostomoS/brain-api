"""Cobertura das pontes de provisionamento em TODO caminho que ativa produto.

O bug que isto trava: uma clínica ativada fora do checkout de signup (cortesia, PATCH do
admin, troca de plano pelo webhook `customer.subscription.*`) ficava com
`tenant.secretaria_provisioned_at = NULL` — sem linha `tenants` na secretarIA — e todo
usuário dela tomava 404 no Brain-Message até alguém abrir `/app/onboarding`.

Cada caminho tem dois testes: (a) a ponte roda e carimba sem visita nenhuma a
`/app/onboarding`; (b) uma secretarIA fora do ar NÃO derruba o caminho principal e deixa
o carimbo NULL para o retry preguiçoso. A tabela de cobertura está em
`docs/CHECKPOINT_provisioning_bridge_coverage.md`.

Ground truth: services/onboarding_sync.py::ensure_products_provisioned.
"""

import time
from uuid import UUID

import pytest

from brain_api.models import Entitlement, Tenant
from brain_api.services import onboarding_sync
from tests.test_billing import _event, _post_webhook, _tenant_ids
from tests.test_courtesy_coupon import _cupom, _le_intent, _registra, _sessao
from tests.test_rbac import ADMIN_EMAIL, ADMIN_PASSWORD, CLINIC_A, CLINIC_B, _bearer, _token


@pytest.fixture
def secretaria(monkeypatch):
    """Substitui `POST /internal/tenants` e deixa o teste escolher o resultado.

    `resultado` True = 2xx; False = o que o cliente devolve para 5xx/timeout/malha sem
    configuração; uma Exception = falha inesperada dentro da ponte.
    """
    registro = {"calls": [], "resultado": True}

    async def fake(tenant_id, **kw):
        registro["calls"].append(tenant_id)
        r = registro["resultado"]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(onboarding_sync.secretaria_provisioning, "provision_tenant", fake)
    return registro


async def _carimbo(tenant_id) -> object:
    async with _sessao() as session:
        tenant = await session.get(Tenant, UUID(str(tenant_id)))
        return tenant.secretaria_provisioned_at


# ── cupom de cortesia (a assimetria original: só chamava a ponte do PreCheck) ──────


async def test_cortesia_com_secretaria_provisiona_sem_visitar_onboarding(client, secretaria):
    await _cupom(code="SECRETARIA100", plan_id="secretaria_basico")
    intent_id = await _registra(client)

    resp = await client.post(
        "/public/courtesy-redemptions", json={"intent_id": intent_id, "code": "SECRETARIA100"}
    )
    assert resp.status_code == 200, resp.text

    intent = await _le_intent(intent_id)
    assert secretaria["calls"] == [intent.tenant_id]
    assert await _carimbo(intent.tenant_id) is not None


async def test_cortesia_so_precheck_nao_chama_secretaria(client, secretaria):
    await _cupom()  # precheck_basic
    intent_id = await _registra(client)

    resp = await client.post(
        "/public/courtesy-redemptions", json={"intent_id": intent_id, "code": "CORTESIA100"}
    )
    assert resp.status_code == 200, resp.text
    assert secretaria["calls"] == []


@pytest.mark.parametrize("falha", [False, RuntimeError("secretaria fora do ar")])
async def test_cortesia_sobrevive_a_secretaria_fora_do_ar(client, secretaria, falha):
    secretaria["resultado"] = falha
    await _cupom(code="SECRETARIA100", plan_id="secretaria_basico")
    intent_id = await _registra(client)

    resp = await client.post(
        "/public/courtesy-redemptions", json={"intent_id": intent_id, "code": "SECRETARIA100"}
    )
    # O resgate segue valendo: clínica ativa e token de onboarding emitido.
    assert resp.status_code == 200, resp.text
    assert resp.json()["onboarding_token"]

    intent = await _le_intent(intent_id)
    assert secretaria["calls"] == [intent.tenant_id]
    assert await _carimbo(intent.tenant_id) is None  # fica para o retry preguiçoso


# ── PATCH /admin/tenants/{id}/entitlements ─────────────────────────────────────────


async def _patch_admin(client, tenant_id: str, body: dict):
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    return await client.patch(
        f"/admin/tenants/{tenant_id}/entitlements", json=body, headers=_bearer(admin_token)
    )


async def test_admin_liga_secretaria_e_provisiona(client, secretaria):
    tenant_b = (await _tenant_ids(client))[CLINIC_B]

    resp = await _patch_admin(client, tenant_b, {"secretaria_enabled": True, "status": "active"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["secretaria_enabled"] is True

    assert secretaria["calls"] == [UUID(tenant_b)]
    assert await _carimbo(tenant_b) is not None


async def test_admin_sem_secretaria_nao_chama_a_ponte(client, secretaria):
    tenant_a = (await _tenant_ids(client))[CLINIC_A]  # só PreCheck

    resp = await _patch_admin(client, tenant_a, {"status": "active"})
    assert resp.status_code == 200, resp.text
    assert secretaria["calls"] == []


@pytest.mark.parametrize("falha", [False, RuntimeError("secretaria fora do ar")])
async def test_admin_sobrevive_a_secretaria_fora_do_ar(client, secretaria, falha):
    secretaria["resultado"] = falha
    tenant_b = (await _tenant_ids(client))[CLINIC_B]

    resp = await _patch_admin(client, tenant_b, {"secretaria_enabled": True, "status": "active"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["secretaria_enabled"] is True
    assert secretaria["calls"] == [UUID(tenant_b)]
    assert await _carimbo(tenant_b) is None

    async with _sessao() as session:
        ent = await session.get(Entitlement, UUID(tenant_b))
        assert ent.secretaria_enabled is True  # a ativação em si foi gravada


# ── webhook customer.subscription.updated (troca de plano que liga a secretarIA) ────


def _assinatura_secretaria(tenant_id: str) -> dict:
    now = int(time.time())
    return {
        "id": "sub_cov",
        "customer": "cus_cov",
        "status": "active",
        "current_period_start": now,
        "current_period_end": now + 30 * 86400,
        "items": {"data": [{"price": {"id": "price_ferro"}, "quantity": 1}]},
        "metadata": {"tenant_id": tenant_id},
    }


async def test_webhook_troca_de_plano_provisiona_secretaria(client, secretaria):
    tenant_b = (await _tenant_ids(client))[CLINIC_B]

    evento = _event("evt_cov_ok", "customer.subscription.updated", _assinatura_secretaria(tenant_b))
    resp = await _post_webhook(client, evento)
    assert resp.status_code == 200, resp.text

    assert secretaria["calls"] == [UUID(tenant_b)]
    assert await _carimbo(tenant_b) is not None

    # Redelivery / evento seguinte: já carimbado, nenhuma chamada nova.
    evento2 = _event(
        "evt_cov_ok2", "customer.subscription.updated", _assinatura_secretaria(tenant_b)
    )
    assert (await _post_webhook(client, evento2)).status_code == 200
    assert secretaria["calls"] == [UUID(tenant_b)]


@pytest.mark.parametrize("falha", [False, RuntimeError("secretaria fora do ar")])
async def test_webhook_sobrevive_a_secretaria_fora_do_ar(client, secretaria, falha):
    secretaria["resultado"] = falha
    tenant_b = (await _tenant_ids(client))[CLINIC_B]

    evento = _event(
        "evt_cov_down", "customer.subscription.updated", _assinatura_secretaria(tenant_b)
    )
    resp = await _post_webhook(client, evento)
    assert resp.status_code == 200, resp.text  # a Stripe não pode ver 5xx por isso
    assert secretaria["calls"] == [UUID(tenant_b)]
    assert await _carimbo(tenant_b) is None

    async with _sessao() as session:
        ent = await session.get(Entitlement, UUID(tenant_b))
        assert ent.secretaria_enabled is True


# ── uma ponte que falha com rollback não pode levar a seguinte junto ────────────────


async def test_rollback_da_ponte_precheck_nao_impede_a_secretaria(client, secretaria, monkeypatch):
    """A ponte do PreCheck faz `session.rollback()` quando falha, o que expira o `tenant`.
    Sem recarregar, a ponte da secretarIA lia um atributo expirado (MissingGreenlet),
    era engolida, e a clínica voltava ao 404 no Brain-Message."""

    async def precheck_que_falha(session, tenant):
        await session.rollback()

    monkeypatch.setattr(onboarding_sync, "ensure_precheck_provisioned", precheck_que_falha)
    await _cupom(code="COMBO100", plan_id="complete_clinic_combo")
    intent_id = await _registra(client)

    resp = await client.post(
        "/public/courtesy-redemptions", json={"intent_id": intent_id, "code": "COMBO100"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["onboarding_token"]

    intent = await _le_intent(intent_id)
    assert secretaria["calls"] == [intent.tenant_id]
    assert await _carimbo(intent.tenant_id) is not None
