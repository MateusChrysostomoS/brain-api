"""GET/PATCH /admin/tenants/{id}/contact-email — e-mail operacional da clínica.

O e-mail mora na secretarIA (`Tenant.contact_email`); o brain-api só o alcança pelo cliente
interno (`services/secretaria_provisioning`). Aqui a chamada de rede é trocada por uma
resposta falsa — a secretarIA real é construída por outra sessão contra o mesmo contrato:

- `GET   /internal/tenants/{id}/contact-email`  -> 200 `{"contact_email": str|null}`; 404.
- `PATCH /internal/tenants/{id}/contact-email`  -> 200 igual; 404; 422 e-mail inválido.

Contrato desta rota em `docs/CHECKPOINT_admin_clinic_contact_email.md`.
"""

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from brain_api.config import get_settings
from brain_api.models import Tenant, User
from brain_api.services import secretaria_provisioning
from tests.test_courtesy_coupon import _sessao
from tests.test_rbac import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    CLINIC_A,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    _bearer,
    _token,
)

EMAIL = "contato@clinica-exemplo.com"


@pytest.fixture
def secretaria(monkeypatch):
    """Troca `_request` (a saída HTTP do cliente interno) e registra cada chamada.

    `estado["resposta"]` é o que a secretarIA "devolve": um `httpx.Response`, ou `None`
    (fora do ar / sem configuração / 401 / 403 — o mesmo que `_request` real devolve).
    """
    estado: dict = {
        "calls": [],
        "resposta": httpx.Response(200, json={"contact_email": EMAIL}),
    }

    async def fake(method, path, *, json=None, params=None, **kw):
        estado["calls"].append((method, path, json))
        return estado["resposta"]

    monkeypatch.setattr(secretaria_provisioning, "_request", fake)
    return estado


async def _admin(client) -> dict:
    return _bearer(await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD))


async def _tenant_id() -> str:
    async with _sessao() as session:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.clinic_name == CLINIC_A))
        ).scalar_one()
        return str(tenant.id)


async def _admin_user_id() -> str:
    async with _sessao() as session:
        user = (await session.execute(select(User).where(User.email == ADMIN_EMAIL))).scalar_one()
        return str(user.id)


def _url(tenant_id: str) -> str:
    return f"/admin/tenants/{tenant_id}/contact-email"


def _upstream(tenant_id: str) -> str:
    return f"/internal/tenants/{tenant_id}/contact-email"


# --- leitura ------------------------------------------------------------------------


async def test_leitura_devolve_o_email_da_secretaria(client, secretaria):
    tid = await _tenant_id()

    resp = await client.get(_url(tid), headers=await _admin(client))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"tenant_id": tid, "contact_email": EMAIL}
    assert secretaria["calls"] == [("GET", _upstream(tid), None)]


async def test_leitura_sem_email_cadastrado_devolve_nulo(client, secretaria):
    secretaria["resposta"] = httpx.Response(200, json={"contact_email": None})
    tid = await _tenant_id()

    resp = await client.get(_url(tid), headers=await _admin(client))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"tenant_id": tid, "contact_email": None}


# --- gravação -----------------------------------------------------------------------


async def test_patch_grava_na_secretaria_e_devolve_o_mesmo_formato_do_get(client, secretaria):
    tid = await _tenant_id()
    secretaria["resposta"] = httpx.Response(200, json={"contact_email": "novo@clinica.com"})

    resp = await client.patch(
        _url(tid), json={"contact_email": "novo@clinica.com"}, headers=await _admin(client)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"tenant_id": tid, "contact_email": "novo@clinica.com"}
    assert secretaria["calls"] == [("PATCH", _upstream(tid), {"contact_email": "novo@clinica.com"})]


@pytest.mark.parametrize(
    ("enviado", "esperado"),
    [
        ("  Contato@Clinica.COM  ", "contato@clinica.com"),
        ("MAIUSCULO@EXEMPLO.COM", "maiusculo@exemplo.com"),
    ],
)
async def test_patch_normaliza_com_trim_e_minusculas_antes_de_enviar(
    client, secretaria, enviado, esperado
):
    tid = await _tenant_id()
    secretaria["resposta"] = httpx.Response(200, json={"contact_email": esperado})

    resp = await client.patch(
        _url(tid), json={"contact_email": enviado}, headers=await _admin(client)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["contact_email"] == esperado
    assert secretaria["calls"][0][2] == {"contact_email": esperado}


@pytest.mark.parametrize("limpar", [None, "", "   "])
async def test_patch_limpa_com_nulo_ou_texto_vazio(client, secretaria, limpar):
    tid = await _tenant_id()
    secretaria["resposta"] = httpx.Response(200, json={"contact_email": None})

    resp = await client.patch(
        _url(tid), json={"contact_email": limpar}, headers=await _admin(client)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"tenant_id": tid, "contact_email": None}
    assert secretaria["calls"] == [("PATCH", _upstream(tid), {"contact_email": None})]


# --- corpo inválido (nunca chega à secretarIA) --------------------------------------


@pytest.mark.parametrize(
    "corpo",
    [
        {},
        {"contact_email": "isto-nao-e-email"},
        {"contact_email": "sem@dominio"},
        {"contact_email": 123},
        {"contact_email": EMAIL, "tenant_id": str(uuid4())},
        {"email": EMAIL},
    ],
)
async def test_corpo_invalido_422_sem_chamar_a_secretaria(client, secretaria, corpo):
    tid = await _tenant_id()

    resp = await client.patch(_url(tid), json=corpo, headers=await _admin(client))

    assert resp.status_code == 422
    assert secretaria["calls"] == []


# --- erros ---------------------------------------------------------------------------


async def test_clinica_desconhecida_no_brain_404_sem_chamar_a_secretaria(client, secretaria):
    admin = await _admin(client)
    desconhecido = str(uuid4())

    leitura = await client.get(_url(desconhecido), headers=admin)
    escrita = await client.patch(_url(desconhecido), json={"contact_email": EMAIL}, headers=admin)

    assert leitura.status_code == escrita.status_code == 404
    assert leitura.json()["detail"] == escrita.json()["detail"] == "Tenant not found"
    assert secretaria["calls"] == []


async def test_secretaria_sem_a_clinica_404_com_codigo_proprio(client, secretaria):
    """Clínica só do PreCheck: existe no brain-api, não tem linha na secretarIA."""
    secretaria["resposta"] = httpx.Response(404, json={"detail": "Tenant not found"})
    tid = await _tenant_id()
    admin = await _admin(client)

    leitura = await client.get(_url(tid), headers=admin)
    escrita = await client.patch(_url(tid), json={"contact_email": EMAIL}, headers=admin)

    assert leitura.status_code == escrita.status_code == 404
    assert leitura.json()["detail"] == escrita.json()["detail"] == "secretaria_tenant_not_found"


@pytest.mark.parametrize(
    "resposta",
    [
        None,  # fora do ar / sem configuração / 401 / 403
        httpx.Response(500, json={"detail": "boom"}),
        httpx.Response(503, text="<html>indisponível</html>"),
        httpx.Response(200, text="não é json"),
        httpx.Response(200, json=["formato", "inesperado"]),
    ],
)
async def test_secretaria_indisponivel_502(client, secretaria, resposta):
    secretaria["resposta"] = resposta
    tid = await _tenant_id()
    admin = await _admin(client)

    leitura = await client.get(_url(tid), headers=admin)
    escrita = await client.patch(_url(tid), json={"contact_email": EMAIL}, headers=admin)

    assert leitura.status_code == escrita.status_code == 502
    assert leitura.json()["detail"] == escrita.json()["detail"] == "secretaria_unavailable"


async def test_malha_sem_configuracao_502_pelo_cliente_real(client, monkeypatch):
    """Sem `SECRETARIA_BASE_URL`/`SECRETARIA_API_KEY` o `_request` real devolve `None`."""
    settings = get_settings()
    monkeypatch.setattr(settings, "SECRETARIA_BASE_URL", "")
    monkeypatch.setattr(settings, "SECRETARIA_API_KEY", "")
    tid = await _tenant_id()

    resp = await client.get(_url(tid), headers=await _admin(client))

    assert resp.status_code == 502
    assert resp.json()["detail"] == "secretaria_unavailable"


async def test_secretaria_recusa_email_422_com_codigo_proprio(client, secretaria):
    secretaria["resposta"] = httpx.Response(422, json={"detail": "bad email"})
    tid = await _tenant_id()

    resp = await client.patch(
        _url(tid), json={"contact_email": EMAIL}, headers=await _admin(client)
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "invalid_contact_email"


# --- RBAC ----------------------------------------------------------------------------


async def test_so_admin_acessa(client, secretaria):
    tid = await _tenant_id()
    gestor = _bearer(await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD))

    assert (await client.get(_url(tid), headers=gestor)).status_code == 403
    assert (
        await client.patch(_url(tid), json={"contact_email": EMAIL}, headers=gestor)
    ).status_code == 403
    assert (await client.get(_url(tid))).status_code == 401
    assert (await client.patch(_url(tid), json={"contact_email": EMAIL})).status_code == 401
    assert secretaria["calls"] == []


# --- log -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("enviado", "limpou"), [("Segredo.Pessoal@Clinica.com", False), (None, True)]
)
async def test_log_tem_ator_clinica_e_cleared_mas_nunca_o_email(
    client, secretaria, enviado, limpou
):
    tid = await _tenant_id()
    admin = await _admin(client)
    ator = await _admin_user_id()
    secretaria["resposta"] = httpx.Response(
        200, json={"contact_email": None if limpou else "segredo.pessoal@clinica.com"}
    )

    with capture_logs() as logs:
        resp = await client.patch(_url(tid), json={"contact_email": enviado}, headers=admin)
        await client.get(_url(tid), headers=admin)

    assert resp.status_code == 200, resp.text
    eventos = [e for e in logs if e["event"] == "admin_contact_email_updated"]
    assert len(eventos) == 1
    assert eventos[0]["actor_user_id"] == ator
    assert eventos[0]["tenant_id"] == tid
    assert eventos[0]["cleared"] is limpou
    assert "segredo.pessoal" not in repr(logs).lower()
    assert "clinica.com" not in repr(logs).lower()


async def test_log_de_falha_tambem_nao_vaza_o_email(client, secretaria):
    secretaria["resposta"] = None
    tid = await _tenant_id()

    with capture_logs() as logs:
        resp = await client.patch(
            _url(tid),
            json={"contact_email": "Segredo.Pessoal@Clinica.com"},
            headers=await _admin(client),
        )

    assert resp.status_code == 502
    assert not [e for e in logs if e["event"] == "admin_contact_email_updated"]
    assert "segredo.pessoal" not in repr(logs).lower()
