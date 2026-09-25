"""POST /admin/tenants — clínica de TESTE criada pelo admin, sem Stripe.

Uma chamada cria tenant + entitlement `active` (só com os produtos escolhidos) + usuário
dono/gestor, e já chama as pontes de provisionamento para os produtos ligados. O tenant
nunca recebe `stripe_customer_id`/`stripe_subscription_id` — é isso que o deixa
invisível a qualquer webhook do Stripe, sem flag de bypass nenhuma.

Contrato em `docs/CHECKPOINT_admin_test_tenant.md`.
"""

import time
from uuid import UUID

import pytest
from sqlalchemy import select

from brain_api.models import Entitlement, Tenant, User
from brain_api.services import billing, onboarding_sync
from tests.test_billing import _event, _post_webhook
from tests.test_courtesy_coupon import _sessao
from tests.test_rbac import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    _bearer,
    _token,
)

OWNER_PASSWORD = "testeclinica1"


def _body(**overrides) -> dict:
    body = {
        "clinic_name": "Clínica Teste Admin",
        "email": "Gestor.Teste@Example.com",
        "name": "Gestor Teste",
        "password": OWNER_PASSWORD,
    }
    body.update(overrides)
    return body


async def _create(client, body: dict):
    admin = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    return await client.post("/admin/tenants", json=body, headers=_bearer(admin))


@pytest.fixture
def pontes(monkeypatch):
    """Substitui as duas chamadas de rede das pontes e registra quem foi chamado."""
    registro = {"secretaria": [], "precheck": []}

    async def fake_secretaria(tenant_id, **kw):
        registro["secretaria"].append(tenant_id)
        return True

    async def fake_precheck(tenant_id, **kw):
        registro["precheck"].append(tenant_id)
        return {"doctor_user_id": 900 + len(registro["precheck"]), "created": True}

    monkeypatch.setattr(
        onboarding_sync.secretaria_provisioning, "provision_tenant", fake_secretaria
    )
    monkeypatch.setattr(onboarding_sync.precheck_provisioning, "provision_clinic", fake_precheck)
    return registro


@pytest.fixture
def stripe_sensor(monkeypatch):
    """Toda saída HTTP para o Stripe passa por `_stripe_post`/`_stripe_get`."""
    chamadas: list[str] = []

    async def _sensor(path, *a, **kw):
        chamadas.append(path)
        raise AssertionError(f"Stripe chamado: {path}")

    monkeypatch.setattr(billing, "_stripe_post", _sensor)
    monkeypatch.setattr(billing, "_stripe_get", _sensor)
    return chamadas


async def _estado(tenant_id: str) -> tuple[Tenant, Entitlement, User]:
    async with _sessao() as session:
        tid = UUID(tenant_id)
        tenant = await session.get(Tenant, tid)
        ent = await session.get(Entitlement, tid)
        user = await session.scalar(select(User).where(User.tenant_id == tid))
        return tenant, ent, user


@pytest.mark.parametrize(
    ("precheck", "secretaria", "plano"),
    [
        (False, False, "free"),
        (True, False, "precheck_advanced"),
        (False, True, "secretaria_basico"),
        (True, True, "complete_clinic_combo"),
    ],
)
async def test_cria_tenant_com_os_produtos_escolhidos(
    client, pontes, stripe_sensor, precheck, secretaria, plano
):
    resp = await _create(client, _body(precheck=precheck, secretaria=secretaria))
    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["clinic_name"] == "Clínica Teste Admin"
    assert data["entitlements"]["status"] == "active"
    assert data["entitlements"]["plan"] == plano
    assert data["entitlements"]["precheck_enabled"] is precheck
    assert data["entitlements"]["secretaria_enabled"] is secretaria
    assert data["entitlements"]["stripe_customer_id"] is None
    assert data["entitlements"]["stripe_subscription_id"] is None
    owner = data["owner"]
    assert owner["email"] == "gestor.teste@example.com"
    assert owner["role"] == "manager"
    assert owner["is_owner"] is True and owner["is_manager"] is True
    assert "password" not in resp.text

    tenant, ent, user = await _estado(data["tenant_id"])
    assert ent.status == "active"
    assert (ent.precheck_enabled, ent.secretaria_enabled) == (precheck, secretaria)
    assert ent.stripe_customer_id is None and ent.stripe_subscription_id is None
    assert tenant.brain_message_enabled is True
    assert user.id == UUID(owner["id"])

    # Ponte chamada e carimbada só para o produto escolhido.
    tid = UUID(data["tenant_id"])
    assert pontes["secretaria"] == ([tid] if secretaria else [])
    assert pontes["precheck"] == ([tid] if precheck else [])
    assert (tenant.secretaria_provisioned_at is not None) is secretaria
    assert (tenant.precheck_provisioned_at is not None) is precheck
    assert stripe_sensor == []


async def test_so_secretaria_entitlements_do_dono_sem_stripe(client, pontes, stripe_sensor):
    """O cenário do pedido, de ponta a ponta: o dono criado loga e lê o próprio
    `GET /entitlements` — secretaria ligada, precheck desligado, zero chamada ao Stripe."""
    resp = await _create(client, _body(secretaria=True))
    assert resp.status_code == 201, resp.text

    owner_token = await _token(client, "gestor.teste@example.com", OWNER_PASSWORD)
    ent = await client.get("/entitlements", headers=_bearer(owner_token))
    assert ent.status_code == 200, ent.text
    body = ent.json()
    assert body["products"]["secretaria"] is True
    assert body["products"]["precheck"] is False
    assert body["status"] == "active"
    assert stripe_sensor == []


async def test_email_ja_cadastrado_409_sem_criar_tenant(client, pontes):
    async with _sessao() as session:
        antes = len((await session.scalars(select(Tenant))).all())

    resp = await _create(client, _body(email=OWNER_A_EMAIL.upper(), secretaria=True))
    assert resp.status_code == 409, resp.text

    async with _sessao() as session:
        assert len((await session.scalars(select(Tenant))).all()) == antes
    assert pontes["secretaria"] == []


@pytest.mark.parametrize(
    "override",
    [
        {"password": "semdigitos"},
        {"password": "12345678"},
        {"password": "a1"},
        {"email": "nao-e-email"},
        {"clinic_name": "   "},
        {"name": ""},
        {"stripe_customer_id": "cus_x"},
    ],
)
async def test_corpo_invalido_422(client, override):
    resp = await _create(client, _body(**override))
    assert resp.status_code == 422, resp.text


async def test_nao_admin_403(client, pontes):
    doctor = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post("/admin/tenants", json=_body(secretaria=True), headers=_bearer(doctor))
    assert resp.status_code == 403, resp.text
    assert pontes["secretaria"] == []


async def test_secretaria_fora_do_ar_nao_derruba_a_criacao(client, monkeypatch):
    """Ponte fail-soft: a clínica nasce ativa, carimbo NULL fica para o retry preguiçoso."""
    chamadas = []

    async def fora_do_ar(tenant_id, **kw):
        chamadas.append(tenant_id)
        return False

    monkeypatch.setattr(onboarding_sync.secretaria_provisioning, "provision_tenant", fora_do_ar)
    resp = await _create(client, _body(secretaria=True))
    assert resp.status_code == 201, resp.text

    tenant, ent, _ = await _estado(resp.json()["tenant_id"])
    assert chamadas == [tenant.id]
    assert ent.status == "active" and ent.secretaria_enabled is True
    assert tenant.secretaria_provisioned_at is None


# ── is_test: a clínica de teste nunca vira cliente do Stripe ────────────────────────


async def test_clinica_nasce_marcada_como_teste_e_admin_ve(client, pontes):
    resp = await _create(client, _body(secretaria=True))
    assert resp.status_code == 201, resp.text
    tid = resp.json()["tenant_id"]
    assert resp.json()["is_test"] is True

    tenant, _, _ = await _estado(tid)
    assert tenant.is_test is True

    admin = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    lista = (await client.get("/admin/tenants", headers=_bearer(admin))).json()["items"]
    marcadas = {t["id"]: t["is_test"] for t in lista}
    assert marcadas[tid] is True
    assert sum(marcadas.values()) == 1  # as clínicas semeadas (reais) seguem False
    detalhe = (await client.get(f"/admin/tenants/{tid}", headers=_bearer(admin))).json()
    assert detalhe["is_test"] is True


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/billing/checkout", {"plan": "secretaria_basico"}),
        ("/billing/portal", None),
        ("/billing/precheck/topup", {"quantity": 10}),
        ("/billing/precheck/upgrade", {"plan": "precheck_basic"}),
    ],
)
async def test_dono_da_clinica_de_teste_nao_abre_nada_no_stripe(
    client, pontes, stripe_sensor, path, body
):
    resp = await _create(client, _body(precheck=True, secretaria=True))
    assert resp.status_code == 201, resp.text
    owner = await _token(client, "gestor.teste@example.com", OWNER_PASSWORD)

    resp = await client.post(path, json=body, headers=_bearer(owner))
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "test_tenant_billing_disabled"
    assert stripe_sensor == []


async def test_clinica_real_nao_e_barrada_pela_trava(client):
    """Controle: a trava só pega `is_test`. Uma clínica real segue o caminho normal (aqui
    503, porque a suíte não configura a chave do Stripe) — nunca o 403 da trava."""
    owner = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/billing/checkout", json={"plan": "secretaria_basico"}, headers=_bearer(owner)
    )
    assert resp.status_code != 403, resp.text
    assert "test_tenant_billing_disabled" not in resp.text


async def test_webhook_com_metadata_da_clinica_de_teste_e_ignorado(client, pontes):
    """Mesmo um evento assinado que nomeia a clínica de teste no metadata não a toca."""
    resp = await _create(client, _body(secretaria=True))
    tid = resp.json()["tenant_id"]
    now = int(time.time())
    obj = {
        "id": "sub_teste",
        "customer": "cus_teste",
        "status": "active",
        "current_period_start": now,
        "current_period_end": now + 30 * 86400,
        "items": {"data": [{"price": {"id": "price_combo"}, "quantity": 1}]},
        "metadata": {"tenant_id": tid},
    }
    resp = await _post_webhook(
        client, _event("evt_teste_meta", "customer.subscription.created", obj)
    )
    assert resp.status_code == 200, resp.text

    _, ent, _ = await _estado(tid)
    assert ent.plan == "secretaria_basico"
    assert ent.precheck_enabled is False
    assert ent.stripe_customer_id is None and ent.stripe_subscription_id is None


async def test_webhook_por_customer_da_clinica_de_teste_e_ignorado(client, pontes):
    """Segunda metade da trava do webhook: a busca por `customer` também recusa um tenant
    de teste (só alcançável se alguém gravar um customer id nele à mão)."""
    resp = await _create(client, _body(secretaria=True))
    tid = resp.json()["tenant_id"]
    async with _sessao() as session:
        ent = await session.get(Entitlement, UUID(tid))
        ent.stripe_customer_id = "cus_manual"
        await session.commit()

    obj = {"id": "sub_x", "customer": "cus_manual", "status": "canceled", "items": {"data": []}}
    resp = await _post_webhook(
        client, _event("evt_teste_cus", "customer.subscription.deleted", obj)
    )
    assert resp.status_code == 200, resp.text

    _, ent, _ = await _estado(tid)
    assert ent.status == "active"
