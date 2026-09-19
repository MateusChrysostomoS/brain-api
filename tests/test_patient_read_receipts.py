"""Delivery state on the Brain-Message channel — the brain-api edge (2026-09-19).

Part 2 of the chain in z_prompts/PLANO_PORTAL_API_MVP.md, wave 3. secretarIA (part 1,
secretarIA/docs/CHECKPOINT_brain_message_status_entrega.md §4) derives each message's state
and moves the poll cursor to `updated_at`; this repo must (a) hand that state to the patient
exactly as it came, (b) never drop a row the product re-sends because its state moved, and
(c) relay the patient's "I have seen up to here" scoped by the SESSION — never by the body.

Every upstream is a fake at the wire level (`test_patient_attachments._mesh`), so what is
asserted is the request httpx really sent.
"""

import json
import uuid

import httpx
import pytest

from brain_api.core.security import create_patient_token, decode_token
from brain_api.models import Tenant
from tests import (
    test_patient_access as base,
    test_patient_attachments as att,
    test_patient_pending_session as pend,
)

pclient = base.pclient
_bearer = base._bearer
_configure_mesh = base._configure_mesh
_login = base._login
_mesh = att._mesh

SECRETARIA_MESSAGES = "/patient-access/threads/secretaria/messages"
SECRETARIA_READ = "/patient-access/threads/secretaria/messages/read"
PRECHECK_READ = "/patient-access/threads/precheck/messages/read"
UPSTREAM_READ = "/internal/brain-message/messages/read"


def _marked(marked=2, applied=True):
    def answer(request):
        return httpx.Response(200, json={"marked": marked, "applied": applied}, request=request)

    return answer


# --- Checklist 1: the poll hands over each message's state exactly as secretarIA sent it -----


async def test_the_poll_relays_each_messages_state_exactly_as_secretaria_sent_it(
    pclient, monkeypatch
):
    """`status`, `delivered_at`, `read_at` and `updated_at` pass through untouched — including
    a state this repo does not know yet: the state is derived ONCE, by secretarIA."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    rows = [
        {
            "id": str(uuid.uuid4()),
            "direction": "inbound",
            "body": "Oi",
            "created_at": "2026-09-19T12:00:00+00:00",
            "status": "entregue",
            "delivered_at": "2026-09-19T12:00:00+00:00",
            "read_at": None,
            "updated_at": "2026-09-19T12:00:00+00:00",
        },
        {
            "id": str(uuid.uuid4()),
            "direction": "outbound",
            "body": "Olá! Como posso ajudar?",
            "created_at": "2026-09-19T12:00:05+00:00",
            "status": "lido",
            "delivered_at": "2026-09-19T12:00:05+00:00",
            "read_at": "2026-09-19T12:01:00+00:00",
            "updated_at": "2026-09-19T12:01:00+00:00",
        },
        {
            "id": str(uuid.uuid4()),
            "direction": "outbound",
            "body": "?",
            "created_at": "2026-09-19T12:02:00+00:00",
            "status": "estado_futuro",  # never re-derived, never validated away
            "delivered_at": None,
            "read_at": None,
            "updated_at": "2026-09-19T12:02:00+00:00",
        },
    ]
    _mesh(monkeypatch, lambda request: httpx.Response(200, json={"data": rows}, request=request))
    login = (await _login(client, sessionmaker, seed.both)).json()

    resp = await client.get(SECRETARIA_MESSAGES, headers=_bearer(login["access_token"]))

    assert resp.status_code == 200, resp.text
    assert resp.json()["payload"]["data"] == rows


async def test_a_file_message_keeps_its_state_while_its_reference_is_rewritten(
    pclient, monkeypatch
):
    """The one key this repo rewrites (`attachment`) does not take the state with it."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    message_id = str(uuid.uuid4())
    row = {
        "id": message_id,
        "direction": "inbound",
        "body": "[anexo: exame.pdf]",
        "attachment": {"content_type": "application/pdf", "size_bytes": 50, "filename": "x.pdf"},
        "status": "lido",
        "delivered_at": "2026-09-19T12:00:00+00:00",
        "read_at": "2026-09-19T12:03:00+00:00",
        "updated_at": "2026-09-19T12:03:00+00:00",
    }
    _mesh(monkeypatch, lambda request: httpx.Response(200, json={"data": [row]}, request=request))
    login = (await _login(client, sessionmaker, seed.both)).json()

    data = (await client.get(SECRETARIA_MESSAGES, headers=_bearer(login["access_token"]))).json()[
        "payload"
    ]["data"]

    assert data[0]["attachment"]["media_path"].endswith(message_id)
    for key in ("status", "delivered_at", "read_at", "updated_at"):
        assert data[0][key] == row[key]


# --- Checklist 4: this repo has no cursor of its own, and never drops a re-sent row ----------


async def test_an_old_cursor_still_gets_the_message_whose_state_moved_after_it(
    pclient, monkeypatch
):
    """Part 1 §5's "poll with an old `since`" scenario, seen from this repo.

    The patient polled at T1 and already has the message (created before T1). The clinic then
    read it — `updated_at` moves past T1 — so the next poll, still with `since=T1`, gets the
    SAME id again with its new state. This repo keeps no cursor: `since` reaches secretarIA
    byte for byte, and nothing here compares it with `created_at` or drops a row it has
    "already sent". A filter on `created_at` at this edge would freeze the tick at "entregue".
    """
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    since = "2026-09-19T12:00:10.123456+00:00"
    message_id = str(uuid.uuid4())
    moved = {
        "id": message_id,
        "direction": "inbound",
        "body": "Oi",
        "created_at": "2026-09-19T12:00:00+00:00",  # BEFORE the cursor
        "status": "lido",
        "delivered_at": "2026-09-19T12:00:00+00:00",
        "read_at": "2026-09-19T12:05:00+00:00",
        "updated_at": "2026-09-19T12:05:00+00:00",  # AFTER the cursor
    }
    # The same id twice (two state moves inside one page) must also survive as sent: dedupe is
    # the client's upsert, not this relay's business.
    again = {**moved, "updated_at": "2026-09-19T12:06:00+00:00"}
    calls = _mesh(
        monkeypatch,
        lambda request: httpx.Response(200, json={"data": [moved, again]}, request=request),
    )
    login = (await _login(client, sessionmaker, seed.both)).json()

    resp = await client.get(
        SECRETARIA_MESSAGES, params={"since": since}, headers=_bearer(login["access_token"])
    )

    assert resp.status_code == 200, resp.text
    assert calls[-1]["params"]["since"] == since
    assert resp.json()["payload"]["data"] == [moved, again]


# --- Checklist 2: the read mark reaches secretarIA scoped by the SESSION ---------------------


@pytest.mark.parametrize(
    ("cursor", "wire"),
    [
        (
            {"up_to_message_id": "7d1c0b6e-8f2a-4c3b-9e5d-2a1b0c9d8e7f"},
            {"up_to_message_id": "7d1c0b6e-8f2a-4c3b-9e5d-2a1b0c9d8e7f"},
        ),
        ({"up_to": "2026-09-19T09:30:00-03:00"}, {"up_to": "2026-09-19T09:30:00-03:00"}),
    ],
    ids=["by-message-id", "by-instant-with-offset"],
)
async def test_the_read_mark_reaches_secretaria_with_the_sessions_clinic_and_handle(
    pclient, monkeypatch, cursor, wire
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _marked(marked=3))
    login = (await _login(client, sessionmaker, seed.both)).json()

    resp = await client.post(SECRETARIA_READ, json=cursor, headers=_bearer(login["access_token"]))

    assert resp.status_code == 200, resp.text
    assert resp.json()["product"] == "secretaria"
    assert resp.json()["payload"] == {"marked": 3, "applied": True}
    assert len(calls) == 1
    call = calls[0]
    assert (call["method"], call["path"]) == ("POST", UPSTREAM_READ)
    assert call["headers"]["x-internal-api-key"]
    # Exactly the keys secretarIA's `extra="forbid"` model takes; tenant and handle are the
    # session's.
    assert json.loads(call["body"]) == {
        "tenant_id": str(seed.both),
        "external_id": login["patient_ref"],
        **wire,
    }


@pytest.mark.parametrize(
    "smuggled",
    [
        {"tenant_id": "00000000-0000-0000-0000-000000000001"},
        {"external_id": "00000000-0000-0000-0000-000000000002"},
        {"patient_ref": "00000000-0000-0000-0000-000000000002"},
        {
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "external_id": "00000000-0000-0000-0000-000000000002",
        },
    ],
    ids=["tenant_id", "external_id", "patient_ref", "both"],
)
async def test_a_body_that_names_a_clinic_or_a_patient_is_refused_and_nothing_is_marked(
    pclient, monkeypatch, smuggled
):
    """Rejected, not silently ignored: a client that believes it can aim the mark elsewhere is
    a bug worth a loud 422 — and nothing reaches secretarIA either way."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _marked())
    login = (await _login(client, sessionmaker, seed.both)).json()
    body = {"up_to": "2026-09-19T12:00:00+00:00", **smuggled}

    resp = await client.post(SECRETARIA_READ, json=body, headers=_bearer(login["access_token"]))

    assert resp.status_code == 422, resp.text
    assert calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {
            "up_to_message_id": "7d1c0b6e-8f2a-4c3b-9e5d-2a1b0c9d8e7f",
            "up_to": "2026-09-19T12:00:00+00:00",
        },
        {"up_to": "2026-09-19T12:00:00"},  # naive: refused, never guessed at
        {"up_to_message_id": "not-a-uuid"},
    ],
    ids=["no-cursor", "two-cursors", "naive-instant", "bad-id"],
)
async def test_the_cursor_is_exactly_one_and_offset_aware_before_any_network_call(
    pclient, monkeypatch, body
):
    """The same rules as secretarIA's model, checked HERE: otherwise its 422 would reach the
    patient as this service's opaque `product_error` 502."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _marked())
    login = (await _login(client, sessionmaker, seed.both)).json()

    resp = await client.post(SECRETARIA_READ, json=body, headers=_bearer(login["access_token"]))

    assert resp.status_code == 422, resp.text
    assert calls == []


# --- Checklist 3: no session, or another clinic's session, marks nothing of this patient ----


@pytest.mark.parametrize("authorization", [None, "Bearer not-a-token", "Basic abc"])
async def test_without_a_patient_session_nothing_is_marked(pclient, monkeypatch, authorization):
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _marked())
    headers = {"Authorization": authorization} if authorization else {}

    resp = await client.post(
        SECRETARIA_READ, json={"up_to": "2026-09-19T12:00:00+00:00"}, headers=headers
    )

    assert resp.status_code == 401, resp.text
    assert calls == []


async def test_another_clinics_session_marks_only_its_own_conversation(pclient, monkeypatch):
    """The same e-mail at two clinics is two patients. B's session marks B's conversation at B's
    clinic — the upstream never sees A's clinic or A's handle, whatever B does."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _marked())
    a = (await _login(client, sessionmaker, seed.both)).json()
    b = (await _login(client, sessionmaker, seed.only_secretaria)).json()
    assert a["patient_ref"] != b["patient_ref"]

    resp = await client.post(
        SECRETARIA_READ,
        json={"up_to": "2026-09-19T12:00:00+00:00"},
        headers=_bearer(b["access_token"]),
    )

    assert resp.status_code == 200, resp.text
    sent = json.loads(calls[0]["body"])
    assert sent["tenant_id"] == str(seed.only_secretaria)
    assert sent["external_id"] == b["patient_ref"]
    assert str(seed.both) not in calls[0]["body"].decode()
    assert a["patient_ref"] not in calls[0]["body"].decode()


async def test_a_token_that_pairs_a_patient_with_another_clinic_marks_nothing(pclient, monkeypatch):
    """A signed token whose clinic disagrees with the patient's row is 401: the clinic of the
    mark is re-read from the identity, never trusted from the claim."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _marked())
    a = (await _login(client, sessionmaker, seed.both)).json()
    sid = decode_token(a["access_token"])["sid"]
    forged = create_patient_token(
        tenant_id=str(seed.only_secretaria),
        patient_ref=a["patient_ref"],
        session_id=sid,
        login_session_id=sid,
    )

    resp = await client.post(
        SECRETARIA_READ, json={"up_to": "2026-09-19T12:00:00+00:00"}, headers=_bearer(forged)
    )

    assert resp.status_code == 401, resp.text
    assert calls == []


async def test_a_clinic_with_the_channel_off_is_refused_before_any_network_call(
    pclient, monkeypatch
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _marked())
    login = (await _login(client, sessionmaker, seed.both)).json()
    async with sessionmaker() as session, session.begin():
        (await session.get(Tenant, seed.both)).brain_message_enabled = False

    resp = await client.post(
        SECRETARIA_READ,
        json={"up_to": "2026-09-19T12:00:00+00:00"},
        headers=_bearer(login["access_token"]),
    )

    assert resp.status_code == 403, resp.text
    assert calls == []


# --- Around the edges -------------------------------------------------------------------------


async def test_a_pending_visitor_marks_its_own_conversation(pclient, monkeypatch):
    """The visitor who has proven nothing yet reads the same thread, so marks it the same way —
    scoped by the visit's own clinic and handle."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _marked(marked=1))
    opened = (await pend._open(client, sessionmaker, seed.both)).json()

    resp = await client.post(
        SECRETARIA_READ,
        json={"up_to": "2026-09-19T12:00:00+00:00"},
        headers=_bearer(opened["pending_token"]),
    )

    assert resp.status_code == 200, resp.text
    sent = json.loads(calls[0]["body"])
    assert (sent["tenant_id"], sent["external_id"]) == (str(seed.both), opened["patient_ref"])


async def test_precheck_threads_do_not_apply_and_cost_no_network(pclient, monkeypatch):
    """PreCheck keeps no state this round: the portal may mark every thread it shows, and a
    PreCheck one answers "does not apply" — like secretarIA for a WhatsApp patient."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _marked())
    login = (await _login(client, sessionmaker, seed.both)).json()

    resp = await client.post(
        PRECHECK_READ,
        json={"up_to": "2026-09-19T12:00:00+00:00"},
        headers=_bearer(login["access_token"]),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["payload"] == {"marked": 0, "applied": False}
    assert calls == []


async def test_a_secretaria_without_the_route_is_the_opaque_product_error(pclient, monkeypatch):
    """Deploy order (secretarIA part 1 first): an old secretarIA answers 404 — surfaced as this
    service's `product_error` 502, the upstream's body never reaching the patient."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    _mesh(
        monkeypatch,
        lambda request: httpx.Response(404, json={"detail": "Not Found"}, request=request),
    )
    login = (await _login(client, sessionmaker, seed.both)).json()

    resp = await client.post(
        SECRETARIA_READ,
        json={"up_to": "2026-09-19T12:00:00+00:00"},
        headers=_bearer(login["access_token"]),
    )

    assert resp.status_code == 502, resp.text
    assert resp.json() == {"detail": "product_error"}
