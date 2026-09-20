"""The automation speaks FIRST (TASK-003 §2) — `POST {SECRETARIA}/internal/brain-message/open`.

Until now a patient who opened a clinic's link landed in an EMPTY conversation: secretarIA's
`Conversation` is born only from an inbound message, and a patient who has not typed anything
has not sent one. brain-api now asks secretarIA to open the conversation and greet, from TWO
places, and the interesting part of this feature is entirely in WHEN it does and does not fire:

* **only on a CREATED visit** — a reload resumes the same visit through `__Host-patient_pending`
  and must not produce a second greeting;
* **only when the resolved product is secretarIA** — `produto=precheck` triggers nothing at all
  (the limitation is deliberate and documented in `api/internal.py::precheck_handoff`);
* **also on `POST /patient-access/clinics`** — the owner's "paciente novo *da clínica*" is
  somebody who already has a Portal account and is opening THIS clinic for the first time; they
  never touch `/pending`;
* **and it can never break or slow the route it hangs off**, which is why it is a background
  task and why the failure tests below assert a `200` while the upstream is on fire.

NOT tested here, because it is not this service's to decide: that two calls produce ONE
greeting. That is secretarIA's idempotence (`200 {"status": "exists"}`), by contract — brain-api
deliberately keeps no state to second-guess it with.
"""

import uuid

import httpx
import pytest

from brain_api.models import Tenant
from brain_api.schemas.entitlement import EntitlementOut
from brain_api.services import message_switchboard

# `pclient` (three clinics over an in-memory DB) is registered by `tests/conftest.py`, so it
# is a fixture NAME here, not an import — importing it too would shadow the parameter.
from tests.test_patient_access import (
    PATIENT_EMAIL,
    _bearer,
    _configure_mesh,
    _peek_code,
    _spy_transport,
)

OPEN_PATH = "/internal/brain-message/open"


# --- helpers -----------------------------------------------------------------------------


def _open_calls(calls: list[dict]) -> list[dict]:
    """Only the greeting hops. Other mesh traffic on the same spy is not this test's subject."""
    return [call for call in calls if str(call["url"]).endswith(OPEN_PATH)]


async def _invite(sessionmaker, tenant_id) -> str:
    async with sessionmaker() as session:
        return (await session.get(Tenant, tenant_id)).patient_invite_code


async def _open(client, sessionmaker, tenant_id, product=None):
    body = {"invite": await _invite(sessionmaker, tenant_id)}
    if product is not None:
        body["product"] = product
    return await client.post("/patient-access/pending", json=body)


async def _account_token(client, sessionmaker) -> str:
    """An account with no clinic yet — the state a returning patient opens a new link in."""
    assert (
        await client.post("/patient-access/request-otp", json={"email": PATIENT_EMAIL})
    ).status_code == 200
    code = await _peek_code(sessionmaker, None, PATIENT_EMAIL)
    resp = await client.post(
        "/patient-access/verify-otp", json={"email": PATIENT_EMAIL, "code": code}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["account_token"]


# --- 1) `POST /pending`: fires on a NEW visit, for secretarIA ------------------------------


async def test_a_new_visit_asks_secretaria_to_open_and_greet(pclient, monkeypatch):
    """The whole feature, on the wire: right path, right header, right handle.

    Asserted on what brain-api SENT rather than on the response, because a greeting that went
    out under the wrong header would look identical from here and be dead in production.
    """
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    resp = await _open(client, sessionmaker, seed.both)
    assert resp.status_code == 200, resp.text

    (call,) = _open_calls(calls)
    assert call["method"] == "POST"
    assert call["base_url"].startswith("http://secretaria:8000")
    # secretarIA's key under secretarIA's header — never PreCheck's pair.
    assert call["headers"]["X-Internal-Api-Key"] == "secretaria-key-AAA"
    assert "X-Internal-Token" not in call["headers"]
    # The handle is the one the browser was just handed: the appointment the patient is about
    # to book lives under it, so a greeting opened under any other id would orphan the thread.
    assert call["json"] == {
        "tenant_id": str(seed.both),
        "external_id": resp.json()["patient_ref"],
        "patient_name": None,
    }


async def test_resuming_the_same_visit_does_not_greet_again(pclient, monkeypatch):
    """F5. The cookie resumes the visit, and a resumed visit is not a new patient.

    This is the single most important negative in the file: secretarIA's idempotence would
    probably absorb a duplicate, but relying on it for a case brain-api can see perfectly well
    would mean every reload spending a mesh round trip to be told "nothing to do".
    """
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    first = await _open(client, sessionmaker, seed.both)
    assert first.status_code == 200
    assert len(_open_calls(calls)) == 1

    # Same client, so the `__Host-patient_pending` cookie rides along — as a reload would.
    second = await _open(client, sessionmaker, seed.both)
    assert second.status_code == 200
    assert second.json()["patient_ref"] == first.json()["patient_ref"], "not a resume"
    assert len(_open_calls(calls)) == 1, "a reload greeted the patient a second time"


async def test_explicit_secretaria_product_greets(pclient, monkeypatch):
    """`?produto=secretaria` — the focused link. Same trigger as the plain one."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    assert (await _open(client, sessionmaker, seed.both, product="secretaria")).status_code == 200
    assert len(_open_calls(calls)) == 1


async def test_a_clinic_with_only_secretaria_greets(pclient, monkeypatch):
    """No `produto` and one product: the resolved tab is secretarIA, so it greets."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    assert (await _open(client, sessionmaker, seed.only_secretaria)).status_code == 200
    assert len(_open_calls(calls)) == 1


# --- 2) The PreCheck link greets NOTHING — the documented limitation -----------------------


async def test_a_precheck_link_triggers_nothing(pclient, monkeypatch):
    """`?produto=precheck` must make NO outbound call — not to secretarIA, not to PreCheck.

    Opening a PreCheck session would need either a route PreCheck does not have or a
    fabricated patient message, and TASK-003 forbids both. The PreCheck thread therefore stays
    empty until the patient writes, exactly as it did before this feature.
    """
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    assert (await _open(client, sessionmaker, seed.both, product="precheck")).status_code == 200
    assert calls == [], "a PreCheck link must not reach the mesh at all"


async def test_a_clinic_with_the_channel_off_greets_nothing(pclient, monkeypatch):
    """A clinic that is not on this channel is refused by `resolve_invite` FIRST (404), so the
    greeting question never even comes up. Pinned anyway: the guard in `_greet_if_secretaria`
    is the second line, and this is the first."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    assert (await _open(client, sessionmaker, seed.channel_off)).status_code == 404
    assert calls == []


def test_default_product_is_none_when_no_product_is_reachable():
    """The second line, on its own: no offered product means nothing to greet into.

    Unreachable through the invite routes today (see above), which is exactly why it is worth
    a direct test — a future caller that resolves a clinic some other way must still be safe.
    """
    from brain_api.schemas.entitlement import ChannelsOut, ProductsOut

    off = EntitlementOut(
        tenant_id=uuid.uuid4(),
        clinic_name="x",
        products=ProductsOut(precheck=True, secretaria=True),
        channels=ChannelsOut(whatsapp=True, brain_message=False),
        plan="free",
        status="active",
    )
    assert message_switchboard.default_product(off) is None


# --- 3) `POST /patient-access/clinics`: new to the CLINIC, not to the Portal ---------------


async def test_adding_a_clinic_to_an_account_greets(pclient, monkeypatch):
    """The second trigger (TASK-003 §2, correction of 2026-09-19).

    This patient has an account already, so they never pass through `/pending` — and they are
    exactly who the owner described as "paciente novo da clínica".
    """
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    token = await _account_token(client, sessionmaker)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    resp = await client.post(
        "/patient-access/clinics",
        headers=_bearer(token),
        json={"invite": await _invite(sessionmaker, seed.both)},
    )
    assert resp.status_code == 200, resp.text

    (call,) = _open_calls(calls)
    assert call["json"]["tenant_id"] == str(seed.both)
    assert call["json"]["external_id"] == resp.json()["patient_ref"]


async def test_adding_a_clinic_with_the_channel_off_greets_nothing(pclient, monkeypatch):
    """Same refusal on this route: a clinic off the channel never enters the account, so it is
    never greeted either."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    token = await _account_token(client, sessionmaker)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    resp = await client.post(
        "/patient-access/clinics",
        headers=_bearer(token),
        json={"invite": await _invite(sessionmaker, seed.channel_off)},
    )
    assert resp.status_code == 404, resp.text
    assert _open_calls(calls) == []


async def test_a_refused_invite_greets_nothing(pclient, monkeypatch):
    """404 before the clinic is resolved — nothing to greet, and nothing is sent."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    token = await _account_token(client, sessionmaker)
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    resp = await client.post(
        "/patient-access/clinics", headers=_bearer(token), json={"invite": "ZZZZZZZZ"}
    )
    assert resp.status_code == 404
    assert calls == []


# --- 4) FIRE AND FORGET: the patient's route survives anything the greeting does -----------
#
# EVERY test in this section asserts TWO things, and the second one is the easy one to forget:
# that the route answered 200, AND that the greeting was actually attempted. Without the
# second, deleting `background_tasks.add_task(...)` outright would leave all of them green —
# `/pending` already answered 200 before this feature existed, so "200" alone proves nothing
# about a failure being survived.


def _boom_on_open(monkeypatch, exc: Exception) -> dict:
    """Make the greeting hop raise `exc`; record that it really was attempted."""
    fired = {"count": 0}
    real_request = httpx.AsyncClient.request

    async def _fake(self, method, url, *, headers=None, json=None, params=None, **kw):
        if str(url).endswith(OPEN_PATH):
            fired["count"] += 1
            raise exc
        return await real_request(
            self, method, url, headers=headers, json=json, params=params, **kw
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake)
    return fired


@pytest.mark.parametrize("upstream_status", [401, 403, 404, 422, 500, 502, 503])
async def test_an_upstream_refusal_never_fails_the_patients_route(
    pclient, monkeypatch, upstream_status: int
):
    """404 is the rollout case (this service live before secretarIA has the route); the rest
    are a misconfigured key and a broken upstream. The patient must get their conversation
    regardless — and, critically, a full `PendingSessionOut`, not a degraded one."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    calls = _spy_transport(monkeypatch, status_code=upstream_status, payload={"detail": "nope"})

    resp = await _open(client, sessionmaker, seed.both)
    assert resp.status_code == 200, resp.text
    assert resp.json()["pending_token"]
    assert resp.json()["patient_ref"]
    assert len(_open_calls(calls)) == 1, "the greeting was never attempted — 200 proves nothing"


async def test_an_unreachable_secretaria_never_fails_the_patients_route(pclient, monkeypatch):
    """secretarIA DOWN — a connect error, not a status code. Still a clean 200."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    fired = _boom_on_open(monkeypatch, httpx.ConnectError("secretaria is down"))

    resp = await _open(client, sessionmaker, seed.both)
    assert resp.status_code == 200, resp.text
    assert fired["count"] == 1, "the connect error branch never ran"


async def test_a_malformed_base_url_never_fails_the_patients_route(pclient, monkeypatch):
    """The failure `httpx.HTTPError` does NOT cover: `httpx.InvalidURL` inherits straight from
    `Exception`, so an operator typo in `SECRETARIA_BASE_URL` would escape a background task
    and surface as "Exception in ASGI application" after a perfectly good response."""
    client, sessionmaker, seed = pclient
    _configure_mesh(monkeypatch)
    fired = _boom_on_open(monkeypatch, httpx.InvalidURL("no host in URL"))

    resp = await _open(client, sessionmaker, seed.both)
    assert resp.status_code == 200, resp.text
    assert fired["count"] == 1, "the InvalidURL branch never ran"


async def test_an_unconfigured_mesh_never_fails_the_patients_route(pclient, monkeypatch):
    """No `SECRETARIA_BASE_URL` at all (the conftest default) — the 503 `_upstream` raises for
    a waiting patient has nobody to raise to here, and is swallowed.

    `calls == []` here CANNOT distinguish "refused before the network" from "never invoked", so
    the unit test below carries the real weight for this branch."""
    client, sessionmaker, seed = pclient
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})

    assert (await _open(client, sessionmaker, seed.both)).status_code == 200
    assert calls == []


async def test_open_conversation_reports_an_unconfigured_mesh(monkeypatch):
    """The branch the route-level test above cannot prove it reached.

    No `_configure_mesh`, so `_upstream` raises its patient-facing 503 — which this function
    must swallow into `OPEN_UNCONFIGURED` rather than let escape into a background task.
    """
    calls = _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})
    outcome = await message_switchboard.open_conversation(
        tenant_id=uuid.uuid4(), patient_ref=str(uuid.uuid4())
    )
    assert outcome == message_switchboard.OPEN_UNCONFIGURED
    assert calls == []


# --- 5) The service function's own contract ------------------------------------------------


@pytest.mark.parametrize(
    ("upstream_status", "expected"),
    [
        (202, message_switchboard.OPEN_QUEUED),
        (200, message_switchboard.OPEN_EXISTS),
        (404, message_switchboard.OPEN_FAILED),
        (500, message_switchboard.OPEN_FAILED),
    ],
)
async def test_open_conversation_outcomes(monkeypatch, upstream_status: int, expected: str):
    """202 and 200 mean genuinely different things (greeted / already had one) and must not
    be folded together — the distinction is the only evidence a log line carries."""
    _configure_mesh(monkeypatch)
    _spy_transport(monkeypatch, status_code=upstream_status, payload={"status": "x"})
    outcome = await message_switchboard.open_conversation(
        tenant_id=uuid.uuid4(), patient_ref=str(uuid.uuid4())
    )
    assert outcome == expected


async def test_open_conversation_never_raises_or_logs_the_handle(monkeypatch):
    """Two properties at once, both load-bearing.

    It must not raise — a background task that throws goes to the server's task log and, in a
    test, to a silently swallowed exception nobody reads. And it must not log the handle: the
    same person's visits to different clinics would line up in the log, which is precisely
    what `services/patient_access.py` refuses to allow elsewhere.
    """
    _configure_mesh(monkeypatch)
    logged: list[tuple] = []

    def _record(*args: object, **kwargs: object) -> None:
        logged.append((args, kwargs))

    for level in ("debug", "info", "warning", "error"):
        monkeypatch.setattr(message_switchboard.logger, level, _record, raising=False)

    handle = str(uuid.uuid4())
    name = "Maria Aparecida de Souza"

    _spy_transport(monkeypatch, status_code=202, payload={"status": "queued"})
    queued = await message_switchboard.open_conversation(
        tenant_id=uuid.uuid4(), patient_ref=handle, patient_name=name
    )
    assert queued == message_switchboard.OPEN_QUEUED

    _spy_transport(monkeypatch, status_code=500, payload={"detail": "boom"})
    failed = await message_switchboard.open_conversation(
        tenant_id=uuid.uuid4(), patient_ref=handle, patient_name=name
    )
    assert failed == message_switchboard.OPEN_FAILED

    # Non-vacuous: this path really did log before we assert on what is absent.
    assert any("brain_message_open_queued" in repr(call) for call in logged), logged
    assert handle not in repr(logged)
    assert name not in repr(logged)
