"""Paged option lists on the Portal — the brain-api edge (TASK-082, session 2).

A Portal card (a list of services, doctors or convenios) carries only its first page; the rest is
fetched on scroll and on search through
`GET /patient-access/threads/{product}/messages/{message_id}/options`. secretarIA owns the data
and decides what the card may list (tasks/TASK-082/CONTRACT.md §c4); this edge owns three things,
and each test below exists to catch one failure of one of them:

- IDENTITY. The clinic and the patient sent upstream are the SESSION's. A browser that names another
  tenant, patient or scope must change nothing, and a card of someone else must answer the one 404 a
  card that never existed answers (the upstream decides ownership from the identity we send).
- INPUT. Bad `q`, `cursor`, `page_size` and card ids die here with a 422, before any network hop.
- LEAKS. An upstream failure never reaches the patient with its own words, and the search text is
  never logged — not by the app, not by httpx (which prints the whole URL), not by uvicorn's access
  log (which prints the query string of the browser's own request).

The upstream is a fake at the wire level (`test_patient_attachments._mesh`) that models the
contract's ownership rule, so "another patient's card is a 404" is proved against a service
that really answers that way, not against a canned 404.
"""

import base64
import json
import logging
import uuid
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.core.security import create_patient_token, decode_token
from brain_api.models import Tenant
from brain_api.services import message_switchboard
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

OTHER_EMAIL = "outra.paciente@exemplo.com"
UPSTREAM = "/internal/brain-message/messages/{card}/options"
SENTINEL = "zq9-termo-secreto"  # a search text no other string in the app can contain


def _url(card, product="secretaria") -> str:
    return f"/patient-access/threads/{product}/messages/{card}/options"


async def _ask(client, card, product="secretaria", headers=None, **body):
    """POST the options request, everything in the JSON body - never in the URL.

    String values are written percent-encoded in the cases below (how they were first typed as
    query strings); they are decoded here, so each case still means the same literal text.
    """
    payload = {k: unquote(v) if isinstance(v, str) else v for k, v in body.items()}
    return await client.post(_url(card, product), json=payload, headers=headers or {})


def _sent(call) -> dict:
    """What brain-api sent upstream: the JSON body of the hop (the URL carries no input)."""
    return json.loads(call["body"] or b"{}")


# --- the fake secretarIA ---------------------------------------------------------------------


def _cursor(offset: int) -> str:
    raw = json.dumps({"v": 1, "o": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _offset(cursor: str) -> int:
    padded = cursor + "=" * (-len(cursor) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded))
    assert data["v"] == 1 and 0 <= data["o"] <= 1000
    return data["o"]


def _catalogue(n: int = 25) -> list[dict]:
    return [
        {"id": f"svc|Serviço {i:02d}", "title": f"Serviço {i:02d}", "description": ""}
        for i in range(n)
    ]


def _secretaria(cards: dict[str, tuple[uuid.UUID, str, list[dict]]]):
    """CONTRACT.md §c4, as far as this edge can observe it.

    `cards[card_id] = (tenant_id, external_id, catalogue)`. A card answers only to the (tenant,
    patient) it was written for — every other asker, and every unknown id, gets the SAME 404.
    """

    def answer(request: httpx.Request) -> httpx.Response:
        def refuse(status, code):
            return httpx.Response(status, json={"detail": {"code": code}}, request=request)

        card_id = request.url.path.split("/")[-2]
        if request.method != "POST" or request.url.query:
            return refuse(405, "method_not_allowed")
        params = json.loads(request.content or b"{}")
        if "tenant_id" not in params or "external_id" not in params:
            return refuse(422, "missing_scope")
        try:
            page_size = int(params.get("page_size", 10))
        except ValueError:
            return refuse(422, "invalid_page_size")
        if not 1 <= page_size <= 10:
            return refuse(422, "invalid_page_size")
        card = cards.get(card_id)
        if card is None or (str(card[0]), card[1]) != (params["tenant_id"], params["external_id"]):
            return refuse(404, "options_not_found")
        offset = 0
        if "cursor" in params:
            try:
                offset = _offset(params["cursor"])
            except Exception:  # noqa: BLE001 - any undecodable cursor is the contract's 422
                return refuse(422, "invalid_cursor")
        needle = params.get("q", "").strip()[:60].casefold()
        matches = [i for i in card[2] if needle in i["title"].casefold()]
        page = matches[offset : offset + page_size]
        more = offset + page_size < len(matches)
        return httpx.Response(
            200,
            json={
                "items": page,
                "next_cursor": _cursor(offset + page_size) if more else None,
                "total": len(matches),
            },
            request=request,
        )

    return answer


async def _patient(client, sessionmaker, tenant_id, email=base.PATIENT_EMAIL) -> dict:
    return (await _login(client, sessionmaker, tenant_id, email)).json()


# --- 1) The happy path, and what is sent upstream --------------------------------------------


async def test_own_card_is_relayed_with_the_sessions_clinic_and_handle(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    card = str(uuid.uuid4())
    calls = _mesh(monkeypatch, _secretaria({card: (seed.both, me["patient_ref"], _catalogue())}))

    resp = await _ask(client, card, headers=_bearer(me["access_token"]))

    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["product"] == "secretaria"
    assert [i["id"] for i in out["payload"]["items"]] == [f"svc|Serviço {i:02d}" for i in range(10)]
    assert out["payload"]["total"] == 25 and out["payload"]["next_cursor"]
    (call,) = calls
    assert (call["method"], call["path"]) == ("POST", UPSTREAM.format(card=card))
    assert _sent(call) == {
        "tenant_id": str(seed.both),
        "external_id": me["patient_ref"],
        "page_size": 10,
    }
    assert call["headers"]["X-Internal-Api-Key"] == "secretaria-key-AAA"
    # The hop's URL carries nothing at all: no ids, no cursor, no search.
    assert call["params"] == {}


async def test_the_cursor_is_opaque_and_goes_on_exactly_as_received(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    card = str(uuid.uuid4())
    calls = _mesh(monkeypatch, _secretaria({card: (seed.both, me["patient_ref"], _catalogue())}))
    headers = _bearer(me["access_token"])

    first = (await _ask(client, card, headers=headers)).json()["payload"]
    second = await _ask(client, card, cursor=first["next_cursor"], headers=headers)
    last = await _ask(client, card, cursor=second.json()["payload"]["next_cursor"], headers=headers)

    assert _sent(calls[1])["cursor"] == first["next_cursor"]
    assert [i["id"] for i in second.json()["payload"]["items"]][0] == "svc|Serviço 10"
    assert [i["id"] for i in last.json()["payload"]["items"]][-1] == "svc|Serviço 24"
    assert last.json()["payload"]["next_cursor"] is None


async def test_search_text_is_trimmed_and_page_size_is_forwarded(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    card = str(uuid.uuid4())
    calls = _mesh(monkeypatch, _secretaria({card: (seed.both, me["patient_ref"], _catalogue())}))

    resp = await _ask(
        client,
        card,
        q="%20%20servi%C3%A7o%2012%20%20",
        page_size=3,
        headers=_bearer(me["access_token"]),
    )

    assert resp.status_code == 200, resp.text
    assert _sent(calls[0])["q"] == "serviço 12"
    assert _sent(calls[0])["page_size"] == 3
    assert [i["id"] for i in resp.json()["payload"]["items"]] == ["svc|Serviço 12"]


@pytest.mark.parametrize("q", ["", "%20%20%20", "%09"])
async def test_an_empty_search_is_no_filter_and_is_not_forwarded(pclient, monkeypatch, q):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    card = str(uuid.uuid4())
    calls = _mesh(monkeypatch, _secretaria({card: (seed.both, me["patient_ref"], _catalogue())}))

    resp = await _ask(client, card, q=q, cursor="", headers=_bearer(me["access_token"]))

    assert resp.status_code == 200, resp.text
    assert "q" not in _sent(calls[0]) and "cursor" not in _sent(calls[0])
    assert resp.json()["payload"]["total"] == 25


async def test_forty_characters_after_trimming_is_the_ceiling_and_is_accepted(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    card = str(uuid.uuid4())
    calls = _mesh(monkeypatch, _secretaria({card: (seed.both, me["patient_ref"], _catalogue())}))

    resp = await _ask(
        client, card, q="%20" * 5 + "a" * 40 + "%20" * 5, headers=_bearer(me["access_token"])
    )

    assert resp.status_code == 200, resp.text
    assert _sent(calls[0])["q"] == "a" * 40


# --- 2) Identity: the browser can name nothing -----------------------------------------------


@pytest.mark.parametrize(
    "named", [{"tenant_id": "other"}, {"external_id": "other"}, {"scope": "x"}, {"kind": "x"}]
)
async def test_a_clinic_patient_or_scope_named_by_the_browser_is_refused(
    pclient, monkeypatch, named
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    card = str(uuid.uuid4())
    calls = _mesh(monkeypatch, _secretaria({card: (seed.both, me["patient_ref"], _catalogue())}))

    resp = await _ask(client, card, headers=_bearer(me["access_token"]), **named)

    assert resp.status_code == 422, resp.text
    assert calls == []


async def test_identity_named_in_the_url_changes_nothing(pclient, monkeypatch):
    """A query string on the POST is not input: the clinic and patient are the session's."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    other = await _patient(client, sessionmaker, seed.only_secretaria, OTHER_EMAIL)
    card = str(uuid.uuid4())
    calls = _mesh(monkeypatch, _secretaria({card: (seed.both, me["patient_ref"], _catalogue())}))

    resp = await client.post(
        _url(card),
        params={"tenant_id": str(seed.only_secretaria), "external_id": other["patient_ref"]},
        json={},
        headers=_bearer(me["access_token"]),
    )

    assert resp.status_code == 200, resp.text
    (call,) = calls
    assert _sent(call) == {
        "tenant_id": str(seed.both),
        "external_id": me["patient_ref"],
        "page_size": 10,
    }


async def test_another_patients_card_in_the_same_clinic_is_the_404_of_a_card_that_never_was(
    pclient, monkeypatch
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    owner = await _patient(client, sessionmaker, seed.both)
    intruder = await _patient(client, sessionmaker, seed.both, OTHER_EMAIL)
    assert owner["patient_ref"] != intruder["patient_ref"]
    card = str(uuid.uuid4())
    calls = _mesh(monkeypatch, _secretaria({card: (seed.both, owner["patient_ref"], _catalogue())}))

    stolen = await _ask(client, card, headers=_bearer(intruder["access_token"]))
    missing = await _ask(client, uuid.uuid4(), headers=_bearer(intruder["access_token"]))

    assert (stolen.status_code, stolen.json()) == (404, {"detail": "options_not_found"})
    assert (missing.status_code, missing.json()) == (404, {"detail": "options_not_found"})
    # The upstream was asked as the INTRUDER, never as the owner: that is what makes it 404.
    assert _sent(calls[0])["external_id"] == intruder["patient_ref"]
    assert owner["patient_ref"] not in json.dumps([_sent(c) for c in calls])


async def test_another_clinics_card_is_a_404(pclient, monkeypatch):
    """The same address at two clinics is two patients: a card of clinic A is invisible to B."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    at_a = await _patient(client, sessionmaker, seed.both)
    at_b = await _patient(client, sessionmaker, seed.only_secretaria)
    card = str(uuid.uuid4())
    calls = _mesh(monkeypatch, _secretaria({card: (seed.both, at_a["patient_ref"], _catalogue())}))

    resp = await _ask(client, card, headers=_bearer(at_b["access_token"]))

    assert (resp.status_code, resp.json()) == (404, {"detail": "options_not_found"})
    assert _sent(calls[0])["tenant_id"] == str(seed.only_secretaria)
    assert str(seed.both) not in json.dumps(_sent(calls[0]))


async def test_a_token_pairing_a_patient_with_another_clinic_reaches_nothing(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    sid = decode_token(me["access_token"])["sid"]
    forged = create_patient_token(
        tenant_id=str(seed.only_secretaria),
        patient_ref=me["patient_ref"],
        session_id=sid,
        login_session_id=sid,
    )
    calls = _mesh(monkeypatch, _secretaria({}))

    resp = await _ask(client, uuid.uuid4(), headers=_bearer(forged))

    assert resp.status_code == 401, resp.text
    assert calls == []


async def test_a_pending_visitor_reads_the_options_of_its_own_card(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    opened = (await pend._open(client, sessionmaker, seed.both)).json()
    card = str(uuid.uuid4())
    calls = _mesh(
        monkeypatch, _secretaria({card: (seed.both, opened["patient_ref"], _catalogue())})
    )

    resp = await _ask(client, card, headers=_bearer(opened["pending_token"]))

    assert resp.status_code == 200, resp.text
    assert (_sent(calls[0])["tenant_id"], _sent(calls[0])["external_id"]) == (
        str(seed.both),
        opened["patient_ref"],
    )


# --- 3) Input: refused here, before any network hop ------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        {"cursor": "a=b"},
        {"cursor": "a%20b"},
        {"cursor": "..%2F..%2Fx"},
        {"cursor": "a" * 201},
        {"cursor": "%C3%A9"},
    ],
    ids=["padding", "space", "traversal", "too-long", "non-ascii"],
)
async def test_a_malformed_cursor_is_a_422_that_names_it(pclient, monkeypatch, query):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    calls = _mesh(monkeypatch, _secretaria({}))

    resp = await _ask(client, uuid.uuid4(), **query, headers=_bearer(me["access_token"]))

    assert (resp.status_code, resp.json()) == (422, {"detail": "invalid_cursor"})
    assert calls == []


async def test_a_cursor_of_exactly_two_hundred_characters_is_still_forwarded(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    calls = _mesh(monkeypatch, _secretaria({}))

    await _ask(client, uuid.uuid4(), cursor="A" * 200, headers=_bearer(me["access_token"]))

    assert _sent(calls[0])["cursor"] == "A" * 200


@pytest.mark.parametrize("q", ["a" * 41, "%20" + "a" * 41 + "%20", "%C3%A1" * 41])
async def test_a_search_over_forty_characters_is_a_422_and_is_not_echoed(pclient, monkeypatch, q):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    calls = _mesh(monkeypatch, _secretaria({}))

    resp = await _ask(client, uuid.uuid4(), q=q, headers=_bearer(me["access_token"]))

    assert (resp.status_code, resp.json()) == (422, {"detail": "invalid_options_query"})
    assert "aaaa" not in resp.text and "%C3" not in resp.text
    assert calls == []


@pytest.mark.parametrize("page_size", ["0", "11", "-1", "abc", "2.5"])
async def test_a_page_size_outside_one_to_ten_is_a_422(pclient, monkeypatch, page_size):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    calls = _mesh(monkeypatch, _secretaria({}))

    resp = await _ask(
        client, uuid.uuid4(), page_size=page_size, headers=_bearer(me["access_token"])
    )

    assert resp.status_code == 422, resp.text
    assert calls == []


@pytest.mark.parametrize("card", ["bad.id", "-leading-dash", "a" * 65, "id%20with%20space"])
async def test_a_malformed_card_id_is_a_422(pclient, monkeypatch, card):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    calls = _mesh(monkeypatch, _secretaria({}))

    resp = await _ask(client, card, headers=_bearer(me["access_token"]))

    assert resp.status_code == 422, resp.text
    assert calls == []


# --- 4) Authentication, entitlement, product -------------------------------------------------


@pytest.mark.parametrize("authorization", [None, "Bearer not-a-token", "Basic abc"])
async def test_without_a_patient_session_nothing_is_fetched(pclient, monkeypatch, authorization):
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _secretaria({}))
    headers = {"Authorization": authorization} if authorization else {}

    resp = await _ask(client, uuid.uuid4(), headers=headers)

    assert resp.status_code == 401, resp.text
    assert calls == []


async def test_authentication_is_checked_before_the_input(pclient, monkeypatch):
    """A stranger learns nothing about the validation rules: 401 first, even for a bad page size."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _secretaria({}))

    resp = await _ask(client, "bad.id", page_size=99, q="a" * 99)

    assert resp.status_code == 401, resp.text
    assert calls == []


async def test_a_clinic_with_the_channel_off_is_refused_before_any_network_call(
    pclient, monkeypatch
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    calls = _mesh(monkeypatch, _secretaria({}))
    async with sessionmaker() as session, session.begin():
        (await session.get(Tenant, seed.both)).brain_message_enabled = False

    resp = await _ask(client, uuid.uuid4(), headers=_bearer(me["access_token"]))

    assert (resp.status_code, resp.json()) == (403, {"detail": "product_unavailable"})
    assert calls == []


@pytest.mark.parametrize("product", ["precheck", "nope"])
async def test_a_product_the_clinic_does_not_have_is_a_403_without_network(
    pclient, monkeypatch, product
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.only_secretaria)
    calls = _mesh(monkeypatch, _secretaria({}))

    resp = await _ask(client, uuid.uuid4(), product, headers=_bearer(me["access_token"]))

    assert (resp.status_code, resp.json()) == (403, {"detail": "product_unavailable"})
    assert calls == []


async def test_precheck_has_no_paged_lists_so_its_answer_is_a_404_without_network(
    pclient, monkeypatch
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)  # this clinic HAS precheck
    calls = _mesh(monkeypatch, _secretaria({}))

    resp = await _ask(client, uuid.uuid4(), "precheck", headers=_bearer(me["access_token"]))

    assert (resp.status_code, resp.json()) == (404, {"detail": "options_not_found"})
    assert calls == []


# --- 5) What the upstream may say, and what the patient may hear -----------------------------


async def test_upstream_404_is_options_not_found_in_our_words(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    _mesh(
        monkeypatch,
        lambda r: httpx.Response(404, json={"detail": "SECRET upstream internals"}, request=r),
    )

    resp = await _ask(client, uuid.uuid4(), headers=_bearer(me["access_token"]))

    assert (resp.status_code, resp.json()) == (404, {"detail": "options_not_found"})
    assert "SECRET" not in resp.text


@pytest.mark.parametrize(
    ("upstream_body", "ours"),
    [
        ({"detail": {"code": "invalid_cursor"}}, "invalid_cursor"),
        ({"detail": "SECRET upstream internals"}, "invalid_options_query"),
        ({"detail": [{"loc": ["query", "page_size"], "msg": "SECRET"}]}, "invalid_options_query"),
        ("not json at all SECRET", "invalid_options_query"),
    ],
    ids=["cursor", "string-detail", "validation-list", "not-json"],
)
async def test_upstream_422_is_one_of_our_two_codes_never_its_own_body(
    pclient, monkeypatch, upstream_body, ours
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)

    def answer(request):
        if isinstance(upstream_body, str):
            return httpx.Response(422, content=upstream_body.encode(), request=request)
        return httpx.Response(422, json=upstream_body, request=request)

    _mesh(monkeypatch, answer)

    resp = await _ask(client, uuid.uuid4(), headers=_bearer(me["access_token"]))

    assert (resp.status_code, resp.json()) == (422, {"detail": ours})
    assert "SECRET" not in resp.text


@pytest.mark.parametrize(
    ("upstream_status", "ours_status", "ours"),
    [
        (500, 502, "product_error"),
        (502, 502, "product_error"),
        (401, 502, "product_error"),  # the shared key is wrong: an operator fact, never OUR 401
        (403, 502, "product_error"),
        (429, 502, "product_error"),
        (503, 503, "product_temporarily_unavailable"),
    ],
)
async def test_every_other_upstream_failure_keeps_its_existing_opaque_answer(
    pclient, monkeypatch, upstream_status, ours_status, ours
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    _mesh(
        monkeypatch,
        lambda r: httpx.Response(upstream_status, json={"detail": "SECRET internals"}, request=r),
    )

    resp = await _ask(client, uuid.uuid4(), headers=_bearer(me["access_token"]))

    assert (resp.status_code, resp.json()) == (ours_status, {"detail": ours})
    assert "SECRET" not in resp.text


async def test_an_unreachable_secretaria_is_a_clean_502(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)

    def down(request):
        raise httpx.ConnectError(
            "connection refused: http://secretaria:8000 SECRET", request=request
        )

    _mesh(monkeypatch, down)

    resp = await _ask(client, uuid.uuid4(), headers=_bearer(me["access_token"]))

    assert (resp.status_code, resp.json()) == (502, {"detail": "product_unreachable"})
    assert "SECRET" not in resp.text and "secretaria:8000" not in resp.text


async def test_an_unconfigured_secretaria_is_a_503_not_a_502(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    me = await _patient(client, sessionmaker, seed.both)  # the mesh is NOT configured here

    resp = await _ask(client, uuid.uuid4(), headers=_bearer(me["access_token"]))

    assert (resp.status_code, resp.json()) == (503, {"detail": "product_channel_unconfigured"})


# --- 6) Cost: pooled connection and rate limit -----------------------------------------------


async def test_the_pooled_connection_is_released_before_the_upstream_hop(pclient, monkeypatch):
    """Scroll fires this in bursts; a DB connection held across the hop would starve the pool."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    card = str(uuid.uuid4())
    answer = _secretaria({card: (seed.both, me["patient_ref"], _catalogue())})
    events: list[str] = []

    real_close = AsyncSession.close

    async def spy_close(self):
        events.append("db_close")
        return await real_close(self)

    monkeypatch.setattr(AsyncSession, "close", spy_close)

    def spying_answer(request):
        events.append("upstream_call")
        return answer(request)

    _mesh(monkeypatch, spying_answer)
    events.clear()

    resp = await _ask(client, card, headers=_bearer(me["access_token"]))

    assert resp.status_code == 200, resp.text
    assert events.index("db_close") < events.index("upstream_call"), events


async def test_the_budget_is_per_patient_and_the_extra_request_is_a_429_without_network(
    pclient, monkeypatch
):
    from brain_api.api.portal import patient_access as router_mod

    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    a = await _patient(client, sessionmaker, seed.both)
    b = await _patient(client, sessionmaker, seed.both, OTHER_EMAIL)
    card_a, card_b = str(uuid.uuid4()), str(uuid.uuid4())
    calls = _mesh(
        monkeypatch,
        _secretaria(
            {
                card_a: (seed.both, a["patient_ref"], _catalogue()),
                card_b: (seed.both, b["patient_ref"], _catalogue()),
            }
        ),
    )
    router_mod._options_limiter._hits.clear()
    monkeypatch.setattr(router_mod._options_limiter, "_limit_getter", lambda: 2)

    statuses = [
        (await _ask(client, card_a, headers=_bearer(a["access_token"]))).status_code
        for _ in range(3)
    ]
    other_patient = await _ask(client, card_b, headers=_bearer(b["access_token"]))

    assert statuses == [200, 200, 429]
    assert other_patient.status_code == 200, "B must not pay for A's scrolling"
    assert len(calls) == 3  # A's third request never left brain-api
    third = await _ask(client, card_a, headers=_bearer(a["access_token"]))
    assert (third.status_code, third.json()) == (429, {"detail": "Too many requests"})


def test_the_budget_defaults_to_sixty_a_minute():
    from brain_api.config import Settings

    assert Settings.model_fields["PATIENT_OPTIONS_RATE_LIMIT_PER_MINUTE"].default == 60


# --- 7) The search text is never logged ------------------------------------------------------


async def test_the_search_text_never_reaches_the_applications_own_logs(
    pclient, monkeypatch, caplog, capsys
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    card = str(uuid.uuid4())
    _mesh(monkeypatch, _secretaria({card: (seed.both, me["patient_ref"], _catalogue())}))
    caplog.set_level(logging.DEBUG)
    capsys.readouterr()

    ok = await _ask(client, card, q=SENTINEL, headers=_bearer(me["access_token"]))
    refused = await _ask(client, card, q=SENTINEL * 5, headers=_bearer(me["access_token"]))

    assert (ok.status_code, refused.status_code) == (200, 422)
    out = capsys.readouterr()
    seen = out.out + out.err + caplog.text
    assert SENTINEL not in seen
    assert SENTINEL not in ok.text and SENTINEL not in refused.text


async def test_the_search_text_never_rides_in_a_url_on_either_hop(monkeypatch, caplog):
    """Every proxy and server logs URLs (the Portal's nginx, the reverse proxy, uvicorn, httpx).

    So the text travels in a BODY on both hops. The outbound hop is made by the REAL client here
    (a mock transport under it): its URL carries nothing, the body carries the text, and the line
    httpx prints at INFO cannot contain it.
    """
    _configure_mesh(monkeypatch)
    real = httpx.AsyncClient
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"items": [], "next_cursor": None, "total": 0})

    monkeypatch.setattr(
        message_switchboard.httpx,
        "AsyncClient",
        lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw),
    )
    caplog.set_level(logging.INFO, logger="httpx")

    await message_switchboard.list_options(
        "secretaria",
        tenant_id=uuid.uuid4(),
        patient_ref="p-1",
        message_id=str(uuid.uuid4()),
        page_size=10,
        q=SENTINEL,
    )

    (request,) = seen
    assert request.method == "POST" and request.url.query == b""
    assert json.loads(request.content)["q"] == SENTINEL, "the upstream must still get the text"
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name == "httpx")
    assert "HTTP Request: POST" in logged and "/options" in logged
    assert SENTINEL not in logged


async def test_there_is_no_url_form_of_the_patients_route(pclient, monkeypatch):
    """A GET with `?q=` does not exist, and a `q` in the POST's query string is not read."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    card = str(uuid.uuid4())
    calls = _mesh(monkeypatch, _secretaria({card: (seed.both, me["patient_ref"], _catalogue())}))
    headers = _bearer(me["access_token"])

    as_get = await client.get(_url(card), params={"q": SENTINEL}, headers=headers)
    in_url = await client.post(_url(card), params={"q": SENTINEL}, json={}, headers=headers)

    assert as_get.status_code == 405
    assert in_url.status_code == 200 and in_url.json()["payload"]["total"] == 25
    assert all("q" not in _sent(c) for c in calls)


# --- 8) Fix round F2 (2026-11-13, D12): the route's hardening --------------------------------


async def _raw(client, card, token, content: bytes, content_type="application/json"):
    """POST a body exactly as written - for shapes `_ask`'s dict cannot express."""
    return await client.post(
        _url(card),
        content=content,
        headers={**_bearer(token), "Content-Type": content_type},
    )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"q": {"a": "zq9-termo-secreto"}}',
        b'{"q": ["zq9-termo-secreto"]}',
        b'{"q": 12345, "cursor": "zq9-termo-secreto"}',
        b'{"cursor": ["zq9-termo-secreto"]}',
        b'["zq9-termo-secreto"]',
        b'"zq9-termo-secreto"',
        b"42",
        b'{"page_size": "zq9-termo-secreto"}',
        b'{"zq9-termo-secreto": "zq9-termo-secreto"}',
        b'{"q": "zq9-termo-secreto',
        b"zq9-termo-secreto",
    ],
    ids=[
        "q-object",
        "q-list",
        "q-number",
        "cursor-list",
        "array",
        "string",
        "number",
        "page-size-text",
        "unknown-key",
        "truncated-json",
        "not-json",
    ],
)
async def test_a_wrong_shaped_body_is_a_422_that_never_echoes_anything_it_was_sent(
    pclient, monkeypatch, raw
):
    """FastAPI's own 422 prints `input` - the rejected value, or the whole body - back."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    calls = _mesh(monkeypatch, _secretaria({}))

    resp = await _raw(client, uuid.uuid4(), me["access_token"], raw)

    assert resp.status_code == 422, resp.text
    assert SENTINEL not in resp.text and "input" not in resp.text
    assert calls == []


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"q": {"a": 1}}', "invalid_options_query"),
        (b'{"q": 5}', "invalid_options_query"),
        (b'{"cursor": ["x"]}', "invalid_cursor"),
        (b'{"cursor": 7}', "invalid_cursor"),
        (b'{"page_size": "abc"}', "invalid_options_request"),
        (b'{"tenant_id": "x"}', "invalid_options_request"),
        (b"[1]", "invalid_options_request"),
    ],
)
async def test_a_wrong_shaped_body_is_told_apart_by_the_field_at_fault_only(
    pclient, monkeypatch, raw, code
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    _mesh(monkeypatch, _secretaria({}))

    resp = await _raw(client, uuid.uuid4(), me["access_token"], raw)

    assert (resp.status_code, resp.json()) == (422, {"detail": code})


@pytest.mark.parametrize(
    "raw",
    [b"[" * 100_000, b'{"q": ' + b"[" * 100_000],
    ids=["nested-array", "nested-under-q"],
)
async def test_a_body_nested_past_the_recursion_limit_is_refused_not_a_500(
    pclient, monkeypatch, raw
):
    """F4 N1: `json.loads` raises RecursionError, not ValueError - it must not escape as a 500."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    calls = _mesh(monkeypatch, _secretaria({}))

    resp = await _raw(client, uuid.uuid4(), me["access_token"], raw)

    assert (resp.status_code, resp.json()) == (422, {"detail": "invalid_options_request"})
    assert calls == []


async def test_a_client_that_goes_away_mid_body_is_a_400_not_a_500():
    """F4 N1: a disconnect while the body is read is Starlette's ClientDisconnect - map it to the
    same 400 the sibling text route answers, never let it escape as a 500."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from brain_api.api.portal import patient_access

    async def receive():
        return {"type": "http.disconnect"}

    request = Request({"type": "http", "method": "POST", "headers": []}, receive)

    with pytest.raises(HTTPException) as refused:
        await patient_access._options_raw(request)

    assert refused.value.status_code == 400
    assert refused.value.detail == patient_access._BODY_UNREADABLE


async def test_the_options_schema_is_still_documented_for_the_body(pclient):
    """Taking the body as `Any` must not make /openapi.json forget what the route accepts."""
    client, _sessionmaker, _seed = pclient

    spec = (await client.get("/openapi.json")).json()

    op = spec["paths"]["/patient-access/threads/{product}/messages/{message_id}/options"]["post"]
    schema = op["requestBody"]["content"]["application/json"]["schema"]
    assert set(schema["properties"]) == {"cursor", "page_size", "q"}


def _nfd(n: int) -> str:
    return "é" * n  # "é" written as two code points


@pytest.mark.parametrize("letters", [20, 21, 40])
async def test_letters_typed_in_nfd_are_measured_as_letters_and_forwarded_composed(
    pclient, monkeypatch, letters
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    calls = _mesh(monkeypatch, _secretaria({}))

    resp = await _ask(client, uuid.uuid4(), q=_nfd(letters), headers=_bearer(me["access_token"]))

    assert resp.status_code != 422, resp.text
    assert _sent(calls[0])["q"] == "é" * letters


@pytest.mark.parametrize("q", ["é" * 41, _nfd(41)], ids=["nfc-41", "nfd-41"])
async def test_forty_one_letters_are_refused_whichever_way_they_were_written(
    pclient, monkeypatch, q
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    calls = _mesh(monkeypatch, _secretaria({}))

    resp = await _ask(client, uuid.uuid4(), q=q, headers=_bearer(me["access_token"]))

    assert (resp.status_code, resp.json()) == (422, {"detail": "invalid_options_query"})
    assert calls == []


async def test_a_decomposed_search_is_composed_before_it_is_trimmed(pclient, monkeypatch):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    calls = _mesh(monkeypatch, _secretaria({}))

    await _ask(client, uuid.uuid4(), q="  " + _nfd(2) + " ", headers=_bearer(me["access_token"]))

    assert _sent(calls[0])["q"] == "éé"


async def test_only_items_cursor_and_total_reach_the_browser(pclient, monkeypatch):
    """An upstream that grows a field (a `scope`, a clinic id...) must not widen what we send."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)

    def answer(request):
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "svc|A",
                        "title": "A",
                        "description": "d",
                        "tenant_id": "LEAK-1",
                        "scope": {"list": "tenant"},
                    }
                ],
                "next_cursor": "abc",
                "total": 1,
                "scope": {"list": "tenant"},
                "tenant_id": "LEAK-2",
            },
            request=request,
        )

    _mesh(monkeypatch, answer)

    resp = await _ask(client, uuid.uuid4(), headers=_bearer(me["access_token"]))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body["payload"]) == {"items", "next_cursor", "total"}
    assert body["payload"]["items"] == [{"id": "svc|A", "title": "A", "description": "d"}]
    assert "LEAK" not in resp.text and "scope" not in resp.text


async def test_a_last_page_keeps_its_null_cursor_and_an_item_without_a_description(
    pclient, monkeypatch
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    _mesh(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={"items": [{"id": "x", "title": "X"}], "next_cursor": None, "total": 1},
            request=r,
        ),
    )

    resp = await _ask(client, uuid.uuid4(), headers=_bearer(me["access_token"]))

    assert resp.json()["payload"] == {
        "items": [{"id": "x", "title": "X"}],
        "next_cursor": None,
        "total": 1,
    }


@pytest.mark.parametrize(
    "upstream",
    [{"items": "nope", "total": 0}, {"total": 0}, ["x"], {"items": ["not-a-dict"], "total": 1}],
    ids=["items-not-a-list", "no-items", "not-an-object", "item-not-an-object"],
)
async def test_an_upstream_page_that_breaks_the_contract_is_the_opaque_502(
    pclient, monkeypatch, upstream
):
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    _mesh(monkeypatch, lambda r: httpx.Response(200, json=upstream, request=r))

    resp = await _ask(client, uuid.uuid4(), headers=_bearer(me["access_token"]))

    assert (resp.status_code, resp.json()) == (502, {"detail": "product_error"})


def _events(logs, name):
    return [e for e in logs if e["event"] == name]


@pytest.mark.parametrize(
    "answer",
    [
        lambda r: httpx.Response(404, json={"detail": "Not Found"}, request=r),
        lambda r: httpx.Response(404, content=b"<html>SECRET</html>", request=r),
        lambda r: httpx.Response(404, json={"detail": {"code": "something_else"}}, request=r),
    ],
    ids=["fastapi-plain-404", "html-404", "other-code"],
)
async def test_an_upstream_404_that_is_not_a_missing_card_is_logged_loudly_but_answered_the_same(
    pclient, monkeypatch, answer
):
    """An old secretarIA with no such route, or a wrong proxy path, looks exactly like someone
    else's card to the patient - the log is the only place a deploy-order mistake shows."""
    from structlog.testing import capture_logs

    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    _mesh(monkeypatch, answer)

    with capture_logs() as logs:
        resp = await _ask(client, uuid.uuid4(), q=SENTINEL, headers=_bearer(me["access_token"]))

    assert (resp.status_code, resp.json()) == (404, {"detail": "options_not_found"})
    (event,) = _events(logs, "switchboard_options_upstream_404_unexpected")
    assert event["log_level"] == "warning" and event["status"] == 404
    assert isinstance(event["code_present"], bool)
    assert "SECRET" not in str(logs) and SENTINEL not in str(logs)


async def test_an_upstream_options_not_found_is_the_normal_case_and_stays_quiet(
    pclient, monkeypatch
):
    from structlog.testing import capture_logs

    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    _mesh(
        monkeypatch,
        lambda r: httpx.Response(404, json={"detail": {"code": "options_not_found"}}, request=r),
    )

    with capture_logs() as logs:
        resp = await _ask(client, uuid.uuid4(), headers=_bearer(me["access_token"]))

    assert resp.status_code == 404
    assert _events(logs, "switchboard_options_upstream_404_unexpected") == []
    assert [e for e in logs if e["log_level"] in ("warning", "error")] == []


@pytest.mark.parametrize(
    ("upstream_code", "mapped"),
    [("invalid_cursor", "invalid_cursor"), ("anything_else", "invalid_options_query")],
)
async def test_the_422_code_that_was_mapped_is_logged_without_the_search(
    pclient, monkeypatch, upstream_code, mapped
):
    from structlog.testing import capture_logs

    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    me = await _patient(client, sessionmaker, seed.both)
    _mesh(
        monkeypatch,
        lambda r: httpx.Response(422, json={"detail": {"code": upstream_code}}, request=r),
    )

    with capture_logs() as logs:
        resp = await _ask(client, uuid.uuid4(), q=SENTINEL, headers=_bearer(me["access_token"]))

    assert (resp.status_code, resp.json()) == (422, {"detail": mapped})
    (event,) = _events(logs, "switchboard_options_upstream_422")
    assert event["mapped"] == mapped and event["status"] == 422
    assert SENTINEL not in str(logs)
