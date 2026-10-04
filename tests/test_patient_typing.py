"""Typing heartbeat: scoped identity, closed body, entitlement and relay contract."""

import json

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.models import Entitlement, Tenant
from tests import (
    test_patient_access as base,
    test_patient_attachments as att,
    test_patient_pending_session as pend,
)

pclient = base.pclient
URL = "/patient-access/threads/secretaria/typing"


@pytest.fixture(autouse=True)
def stub_setup_email(monkeypatch):
    """OTP setup uses the mesh too; answer at HTTP boundary, never real DNS."""
    original_send = httpx.AsyncClient.send

    async def send(self, request, **kwargs):
        if request.url.path == "/internal/notifications/email":
            return httpx.Response(202, json={"queued": True}, request=request)
        return await original_send(self, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)


_mesh = att._mesh


async def test_the_patient_beat_is_relayed_with_the_sessions_scope(pclient, monkeypatch):
    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, lambda r: httpx.Response(200, json={"applied": True}, request=r))
    login = (await base._login(client, maker, seed.both)).json()
    response = await client.post(URL, headers=base._bearer(login["access_token"]))
    assert response.status_code == 200
    assert response.json()["payload"] == {"applied": True}
    assert calls[0]["path"] == "/internal/brain-message/typing"
    assert calls[0]["headers"]["x-internal-api-key"] == "secretaria-key-AAA"
    assert json.loads(calls[0]["body"]) == {
        "tenant_id": str(seed.both),
        "external_id": login["patient_ref"],
    }


async def test_precheck_answers_without_the_network(pclient, monkeypatch):
    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, lambda r: httpx.Response(200, json={"applied": True}, request=r))
    login = (await base._login(client, maker, seed.both)).json()
    response = await client.post(
        "/patient-access/threads/precheck/typing", headers=base._bearer(login["access_token"])
    )
    assert response.status_code == 200
    assert response.json()["payload"] == {"applied": False}
    assert calls == []


@pytest.mark.parametrize(
    "body", [{"tenant_id": "x"}, {"external_id": "x"}, {"patient_ref": "x"}, {"by": "staff"}]
)
async def test_body_cannot_name_identity_or_typer(pclient, monkeypatch, body):
    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, lambda r: httpx.Response(200, json={"applied": True}, request=r))
    login = (await base._login(client, maker, seed.both)).json()
    response = await client.post(URL, json=body, headers=base._bearer(login["access_token"]))
    assert response.status_code == 422
    assert calls == []


async def test_without_a_session_no_beat_is_sent(pclient, monkeypatch):
    client, _, _ = pclient
    base._configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, lambda r: httpx.Response(200, json={"applied": True}, request=r))
    assert (await client.post(URL)).status_code == 401
    assert calls == []


async def test_poll_relays_typing_fields_untouched(pclient, monkeypatch):
    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    payload = {
        "data": [],
        "has_more": False,
        "typing": True,
        "typing_by": "staff",
        "accepts_typing": True,
    }
    _mesh(monkeypatch, lambda r: httpx.Response(200, json=payload, request=r))
    login = (await base._login(client, maker, seed.both)).json()
    response = await client.get(
        "/patient-access/threads/secretaria/messages", headers=base._bearer(login["access_token"])
    )
    assert response.json()["payload"] == payload


async def test_pending_visit_beats_with_its_own_scope(pclient, monkeypatch):
    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, lambda r: httpx.Response(200, json={"applied": True}, request=r))
    opened = (await pend._open(client, maker, seed.both)).json()
    calls.clear()
    response = await client.post(URL, json={}, headers=base._bearer(opened["pending_token"]))
    assert response.status_code == 200
    assert json.loads(calls[0]["body"]) == {
        "tenant_id": str(seed.both),
        "external_id": opened["patient_ref"],
    }


@pytest.mark.parametrize("gate", ["channel", "product"])
async def test_entitlement_denial_happens_before_network(pclient, monkeypatch, gate):
    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, lambda r: httpx.Response(200, json={"applied": True}, request=r))
    login = (await base._login(client, maker, seed.both)).json()
    async with maker() as session, session.begin():
        if gate == "channel":
            (await session.get(Tenant, seed.both)).brain_message_enabled = False
        else:
            ent = await session.scalar(
                select(Entitlement).where(Entitlement.tenant_id == seed.both)
            )
            ent.secretaria_enabled = False
    response = await client.post(URL, headers=base._bearer(login["access_token"]))
    assert response.status_code == 403
    assert calls == []


async def test_database_connection_released_before_upstream(pclient, monkeypatch):
    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    login = (await base._login(client, maker, seed.both)).json()
    sessions_closed = []
    close = AsyncSession.close

    async def tracked_close(self):
        sessions_closed.append(self)
        await close(self)

    def answer(request):
        assert sessions_closed, "route must release DB session before mesh hop"
        assert all(not session.in_transaction() for session in sessions_closed)
        return httpx.Response(200, json={"applied": True}, request=request)

    monkeypatch.setattr(AsyncSession, "close", tracked_close)
    _mesh(monkeypatch, answer)
    response = await client.post(URL, headers=base._bearer(login["access_token"]))
    assert response.status_code == 200
