"""GET/PUT /admin/tenants/{id}/professional-capacity — quantos profissionais a clínica pode
manter ativos (TASK-063).

O número é o `limits["professionals"]` que a secretarIA lê fresco a cada convite/ativação;
o add-on `multi_professional` precisa estar ligado ou o convite recebe 403. Contrato em
`docs/CHECKPOINT_admin_professional_capacity.md`.
"""

from uuid import UUID, uuid4

import pytest

from brain_api.models import Entitlement
from brain_api.services import entitlements as entitlements_service, professional_capacity
from tests.test_admin_tenant_create import _body, _create, pontes  # noqa: F401 (fixture)
from tests.test_courtesy_coupon import _sessao
from tests.test_rbac import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    _bearer,
    _token,
)

# As pontes de provisionamento são chamadas de rede; toda clínica criada aqui passa por elas.
pytestmark = pytest.mark.usefixtures("pontes")


@pytest.fixture
def roster(monkeypatch):
    """Substitui a leitura da equipe na secretarIA; `None` simula secretarIA fora do ar."""
    estado = {"rows": [], "calls": 0}

    async def fake(tenant_id):
        estado["calls"] += 1
        return estado["rows"]

    monkeypatch.setattr(professional_capacity.secretaria_provisioning, "get_professionals", fake)
    return estado


def _ativos(n: int, inativos: int = 0) -> list[dict]:
    return [{"id": str(uuid4()), "is_active": True} for _ in range(n)] + [
        {"id": str(uuid4()), "is_active": False} for _ in range(inativos)
    ]


async def _clinica(client, *, secretaria=True, precheck=False) -> str:
    resp = await _create(client, _body(secretaria=secretaria, precheck=precheck))
    assert resp.status_code == 201, resp.text
    return resp.json()["tenant_id"]


async def _admin(client) -> dict:
    return _bearer(await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD))


def _url(tenant_id: str) -> str:
    return f"/admin/tenants/{tenant_id}/professional-capacity"


async def _ent(tenant_id: str) -> Entitlement:
    async with _sessao() as session:
        return await session.get(Entitlement, UUID(tenant_id))


async def test_leitura_mostra_limite_do_plano_e_ativos_ao_vivo(client, roster):
    tid = await _clinica(client)
    roster["rows"] = _ativos(1, inativos=2)

    resp = await client.get(_url(tid), headers=await _admin(client))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "tenant_id": tid,
        "max_professionals": 1,
        "multi_professional": False,
        "active_professionals": 1,
        "editable": True,
        "blocked_reason": None,
        "max_allowed": 50,
    }


async def test_definir_capacidade_liga_addon_e_grava_o_limite_que_a_secretaria_le(client, roster):
    tid = await _clinica(client)
    antes = dict((await _ent(tid)).limits)
    roster["rows"] = _ativos(1)

    resp = await client.put(_url(tid), json={"max_professionals": 3}, headers=await _admin(client))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["max_professionals"] == 3
    assert body["multi_professional"] is True
    assert body["active_professionals"] == 1

    ent = await _ent(tid)
    assert ent.addons["multi_professional"] is True
    assert ent.limits["professionals"] == 3
    # Os outros limites ficam como estavam.
    assert {k: v for k, v in ent.limits.items() if k != "professionals"} == {
        k: v for k, v in antes.items() if k != "professionals"
    }
    # O que a rota interna (a que a secretarIA consulta) devolve.
    async with _sessao() as session:
        visto = await entitlements_service.resolve_entitlement(session, UUID(tid))
    assert visto.limits["professionals"] == 3
    assert visto.addons["multi_professional"] is True


async def test_definir_e_idempotente(client, roster):
    tid = await _clinica(client)
    admin = await _admin(client)
    primeira = await client.put(_url(tid), json={"max_professionals": 2}, headers=admin)
    segunda = await client.put(_url(tid), json={"max_professionals": 2}, headers=admin)
    assert primeira.status_code == segunda.status_code == 200
    assert primeira.json() == segunda.json()


async def test_recusa_limite_abaixo_da_equipe_ativa_sem_gravar(client, roster):
    tid = await _clinica(client)
    admin = await _admin(client)
    assert (
        await client.put(_url(tid), json={"max_professionals": 5}, headers=admin)
    ).status_code == 200
    roster["rows"] = _ativos(4, inativos=3)

    resp = await client.put(_url(tid), json={"max_professionals": 3}, headers=admin)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "below_active_professionals"
    assert (await _ent(tid)).limits["professionals"] == 5
    # Igual à equipe ativa é permitido (inativos não contam).
    assert (
        await client.put(_url(tid), json={"max_professionals": 4}, headers=admin)
    ).status_code == 200


async def test_equipe_ilegivel_nao_bloqueia_e_responde_ativos_nulo(client, roster):
    tid = await _clinica(client)
    roster["rows"] = None

    resp = await client.put(_url(tid), json={"max_professionals": 2}, headers=await _admin(client))

    assert resp.status_code == 200, resp.text
    assert resp.json()["active_professionals"] is None
    assert (await _ent(tid)).limits["professionals"] == 2


async def test_clinica_sem_secretaria_nao_edita_e_nem_consulta_a_secretaria(client, roster):
    tid = await _clinica(client, secretaria=False, precheck=True)
    admin = await _admin(client)

    leitura = await client.get(_url(tid), headers=admin)
    assert leitura.status_code == 200
    assert leitura.json()["editable"] is False
    assert leitura.json()["blocked_reason"] == "secretaria_disabled"
    assert leitura.json()["active_professionals"] is None

    escrita = await client.put(_url(tid), json={"max_professionals": 2}, headers=admin)
    assert escrita.status_code == 409
    assert escrita.json()["detail"] == "secretaria_disabled"
    assert roster["calls"] == 0


async def test_clinica_com_assinatura_stripe_nao_edita(client, roster):
    tid = await _clinica(client)
    async with _sessao() as session:
        ent = await session.get(Entitlement, UUID(tid))
        ent.stripe_subscription_id = "sub_test_123"
        await session.commit()
    admin = await _admin(client)

    assert (await client.get(_url(tid), headers=admin)).json()["blocked_reason"] == "stripe_managed"
    resp = await client.put(_url(tid), json={"max_professionals": 4}, headers=admin)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "stripe_managed"
    assert (await _ent(tid)).limits["professionals"] == 1


async def test_ligar_desligar_produto_depois_preserva_a_capacidade(client, roster):
    tid = await _clinica(client, secretaria=True, precheck=True)
    admin = await _admin(client)
    await client.put(_url(tid), json={"max_professionals": 4}, headers=admin)

    patch = await client.patch(
        f"/admin/tenants/{tid}/entitlements", json={"precheck_enabled": False}, headers=admin
    )

    assert patch.status_code == 200, patch.text
    assert (await _ent(tid)).limits["professionals"] == 4


async def test_editar_outro_addon_pelo_patch_preserva_a_capacidade(client, roster):
    tid = await _clinica(client)
    admin = await _admin(client)
    await client.put(_url(tid), json={"max_professionals": 5}, headers=admin)

    patch = await client.patch(
        f"/admin/tenants/{tid}/entitlements",
        json={"addons": {"multi_professional": True, "reactivation_pack": True}},
        headers=admin,
    )

    assert patch.status_code == 200, patch.text
    assert (await _ent(tid)).limits["professionals"] == 5


async def test_desligar_multi_professional_pelo_patch_volta_ao_limite_do_plano(client, roster):
    tid = await _clinica(client)
    admin = await _admin(client)
    await client.put(_url(tid), json={"max_professionals": 5}, headers=admin)

    await client.patch(
        f"/admin/tenants/{tid}/entitlements",
        json={"addons": {"multi_professional": False}},
        headers=admin,
    )

    ent = await _ent(tid)
    assert ent.addons["multi_professional"] is False
    assert ent.limits["professionals"] == 1


async def test_tela_mostra_o_mesmo_limite_que_a_secretaria_le_sem_a_chave_gravada(client, roster):
    tid = await _clinica(client)
    async with _sessao() as session:
        ent = await session.get(Entitlement, UUID(tid))
        ent.limits = {"messages": 400}  # linha antiga, sem a chave `professionals`
        await session.commit()

    resp = await client.get(_url(tid), headers=await _admin(client))

    async with _sessao() as session:
        visto = await entitlements_service.resolve_entitlement(session, UUID(tid))
    assert resp.json()["max_professionals"] == visto.limits["professionals"] == 1


@pytest.mark.parametrize(
    "corpo",
    [
        {"max_professionals": 0},
        {"max_professionals": 51},
        {"max_professionals": "três"},
        {},
        {"max_professionals": 2, "addons": {"multi_professional": False}},
        {"max_professionals": 2, "tenant_id": str(uuid4())},
    ],
)
async def test_corpo_invalido_e_recusado(client, roster, corpo):
    tid = await _clinica(client)
    resp = await client.put(_url(tid), json=corpo, headers=await _admin(client))
    assert resp.status_code == 422
    assert (await _ent(tid)).limits["professionals"] == 1


async def test_clinica_inexistente_404(client, roster):
    admin = await _admin(client)
    assert (await client.get(_url(str(uuid4())), headers=admin)).status_code == 404
    resp = await client.put(_url(str(uuid4())), json={"max_professionals": 2}, headers=admin)
    assert resp.status_code == 404


async def test_so_admin_acessa(client, roster):
    tid = await _clinica(client)
    gestor = _bearer(await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD))

    assert (await client.get(_url(tid), headers=gestor)).status_code == 403
    assert (
        await client.put(_url(tid), json={"max_professionals": 9}, headers=gestor)
    ).status_code == 403
    assert (await client.get(_url(tid))).status_code == 401
    assert (await _ent(tid)).limits["professionals"] == 1
