"""`POST /patient-access/threads/{product}/enter` — the Portal speaks first on ENTRY.

Owner, 2026-10-05: the first message must arrive as soon as the patient enters the Portal, not
after they type. Before this route only a NEW visit (`POST /pending`) or a link opened with an
account cookie reached secretarIA's `/internal/brain-message/open`; a refresh, a clinic picked
from the list or a returning account never did. secretarIA decides WHAT (if anything) to say;
these tests pin only what brain-api owns: the scope, the product gate and the wire call.
"""

import json

import httpx

from tests import (
    test_patient_access as base,
    test_patient_pending_session as pend,
)

pclient = base.pclient
URL = "/patient-access/threads/secretaria/enter"
OPEN_PATH = "/internal/brain-message/open"


def _spy(monkeypatch, status_code=200, payload=None):
    """Record every mesh hop to the open route; answer the rest of the mesh with `{}`.

    The test client's own ASGI hop is not a mesh URL and passes through untouched.
    """
    calls: list[dict] = []
    real_send = httpx.AsyncClient.send

    async def _fake_send(self, request, **kwargs):
        url = str(request.url)
        if "secretaria:8000" not in url and "precheck:8000" not in url:
            return await real_send(self, request, **kwargs)
        if request.url.path == OPEN_PATH:
            calls.append(
                {
                    "headers": {k.lower(): v for k, v in request.headers.items()},
                    "json": json.loads(request.content or b"{}"),
                }
            )
            return httpx.Response(
                status_code, json=payload or {"status": "exists"}, request=request
            )
        return httpx.Response(202, json={"queued": True}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", _fake_send)
    return calls


async def _logged_in(client, maker, tenant_id) -> dict:
    login = await base._login(client, maker, tenant_id)
    assert login.status_code == 200, login.text
    return login.json()


async def test_a_logged_in_patient_entering_asks_secretaria_to_speak(pclient, monkeypatch):
    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    calls = _spy(monkeypatch)
    login = await _logged_in(client, maker, seed.both)
    before = len(calls)

    response = await client.post(URL, headers=base._bearer(login["access_token"]))

    assert response.status_code == 200, response.text
    assert response.json()["payload"] == {"scheduled": True}
    (call,) = calls[before:]
    assert call["headers"]["x-internal-api-key"] == "secretaria-key-AAA"
    assert call["json"]["tenant_id"] == str(seed.both)
    assert call["json"]["external_id"] == login["patient_ref"]


async def test_every_entry_is_forwarded_secretaria_dedupes(pclient, monkeypatch):
    """Unlike `/pending`, a reload IS forwarded: whether to speak is secretarIA's call
    (silent mid-flow and right after the last message), so brain-api keeps no state here."""
    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    calls = _spy(monkeypatch)
    login = await _logged_in(client, maker, seed.both)
    before = len(calls)
    for _ in range(2):
        response = await client.post(URL, headers=base._bearer(login["access_token"]))
        assert response.status_code == 200
    assert len(calls) - before == 2


async def test_explicit_reminder_is_forwarded_with_authenticated_identity(pclient, monkeypatch):
    from uuid import uuid4

    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    calls = _spy(monkeypatch)
    login = await _logged_in(client, maker, seed.both)
    context = {"source": "reminder_link", "reminder_id": str(uuid4())}
    response = await client.post(URL, headers=base._bearer(login["access_token"]),
                                 json={"entry_context": context})
    assert response.status_code == 200, response.text
    assert calls[-1]["json"]["entry_context"] == context
    assert calls[-1]["json"]["external_id"] == login["patient_ref"]
    assert calls[-1]["json"]["tenant_id"] == str(seed.both)


async def test_reminder_after_anonymous_otp_uses_existing_patient_identity(pclient, monkeypatch):
    from uuid import UUID, uuid4

    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    calls = _spy(monkeypatch)
    existing = await pend._seed_identity(maker, seed.both, pend.PATIENT_EMAIL)
    opened = (await pend._open(client, maker, seed.both)).json()
    await pend._claim(client, monkeypatch, seed.both, UUID(opened["patient_ref"]))
    login = await pend._prove(client, maker, opened["pending_token"])
    assert login.status_code == 200, login.text
    assert login.json()["patient_ref"] == str(existing)
    context = {"source": "reminder_link", "reminder_id": str(uuid4())}
    response = await client.post(URL, headers=base._bearer(login.json()["access_token"]),
                                 json={"entry_context": context})
    assert response.status_code == 200, response.text
    assert calls[-1]["json"]["external_id"] == str(existing)
    assert calls[-1]["json"]["external_id"] != opened["patient_ref"]
    assert calls[-1]["json"]["entry_context"] == context


async def test_a_pending_visitor_entering_is_forwarded_too(pclient, monkeypatch):
    """F5 on a visit whose greeting never landed: the empty thread gets its greeting now."""
    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    calls = _spy(monkeypatch, status_code=202, payload={"status": "queued"})
    visit = await pend._open(client, maker, seed.both)
    assert visit.status_code == 200, visit.text
    before = len(calls)

    response = await client.post(URL, headers=base._bearer(visit.json()["pending_token"]))

    assert response.status_code == 200, response.text
    (call,) = calls[before:]
    assert call["json"]["external_id"] == visit.json()["patient_ref"]


async def test_precheck_entry_sends_nothing(pclient, monkeypatch):
    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    calls = _spy(monkeypatch)
    login = await _logged_in(client, maker, seed.both)
    before = len(calls)
    response = await client.post(
        "/patient-access/threads/precheck/enter", headers=base._bearer(login["access_token"])
    )
    assert response.status_code == 200
    assert response.json()["payload"] == {"scheduled": False}
    assert calls[before:] == []


async def test_body_cannot_name_an_identity(pclient, monkeypatch):
    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    calls = _spy(monkeypatch)
    login = await _logged_in(client, maker, seed.both)
    before = len(calls)
    response = await client.post(
        URL, json={"external_id": "x"}, headers=base._bearer(login["access_token"])
    )
    assert response.status_code == 422
    assert calls[before:] == []


async def test_without_a_session_nothing_is_sent(pclient, monkeypatch):
    client, _, _ = pclient
    base._configure_mesh(monkeypatch)
    calls = _spy(monkeypatch)
    assert (await client.post(URL)).status_code == 401
    assert calls == []


async def test_an_upstream_failure_never_breaks_the_entry(pclient, monkeypatch):
    client, maker, seed = pclient
    base._configure_mesh(monkeypatch)
    _spy(monkeypatch, status_code=500, payload={"detail": "boom"})
    login = await _logged_in(client, maker, seed.both)
    response = await client.post(URL, headers=base._bearer(login["access_token"]))
    assert response.status_code == 200
    assert response.json()["payload"] == {"scheduled": True}
