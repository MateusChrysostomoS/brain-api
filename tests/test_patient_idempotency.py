"""Idempotent patient sends (2026-09-25) — `Idempotency-Key` on POST /threads/{product}/messages.

The portal retries a send on its own when a mobile network drops the answer. A retry of a
message the product ALREADY received must not reach it twice: to PreCheck's questionnaire a
second copy is a second answer. Everything asserted here is about the product's inbound —
how many times it was called — because that, not the patient's 200, is the duplicate.
"""

import asyncio

import httpx
import pytest

from brain_api.core import idempotency
from tests import test_patient_access as base
from tests.test_patient_attachments import _mesh, _patient, _png

pclient = base.pclient
_bearer = base._bearer
_configure_mesh = base._configure_mesh

PRECHECK = "/patient-access/threads/precheck/messages"
SECRETARIA = "/patient-access/threads/secretaria/messages"
KEY = "3f2b8c1e-9d4a-4b7e-8f60-1a2b3c4d5e6f"
_TURN = {"messages": [], "status": "question", "state": "ACTIVE"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    idempotency.patient_sends.clear()
    yield
    idempotency.patient_sends.clear()


def _turn(request):
    return httpx.Response(200, json=_TURN, request=request)


async def _send(client, token, key=None, text="Sim", url=PRECHECK):
    headers = _bearer(token)
    if key is not None:
        headers[idempotency.HEADER] = key
    return await client.post(url, headers=headers, json={"text": text})


def _inbound(calls):
    return [c for c in calls if c["path"].startswith("/internal/brain-message/inbound")]


# --- 1) The route ----------------------------------------------------------------------------


async def test_a_repeated_key_is_relayed_once_and_answered_twice(pclient, monkeypatch):
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _turn)
    token = (await _patient(pclient))["access_token"]

    first = await _send(client, token, KEY)
    second = await _send(client, token, KEY)

    assert first.status_code == second.status_code == 200, second.text
    assert len(_inbound(calls)) == 1, "the retry reached PreCheck a second time"
    assert second.json() == first.json()  # the same answer, `at` included
    assert idempotency.REPLAYED_HEADER not in first.headers
    assert second.headers[idempotency.REPLAYED_HEADER] == "true"


async def test_different_keys_are_different_messages(pclient, monkeypatch):
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _turn)
    token = (await _patient(pclient))["access_token"]

    assert (await _send(client, token, KEY)).status_code == 200
    assert (await _send(client, token, KEY.replace("3f", "4f"))).status_code == 200
    assert len(_inbound(calls)) == 2


async def test_without_a_key_every_send_is_relayed_as_before(pclient, monkeypatch):
    """Today's front sends no key: nothing about it changes."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _turn)
    token = (await _patient(pclient))["access_token"]

    first = await _send(client, token)
    second = await _send(client, token)
    assert first.status_code == second.status_code == 200
    assert len(_inbound(calls)) == 2
    assert idempotency.REPLAYED_HEADER not in second.headers


async def test_a_failure_is_not_remembered(pclient, monkeypatch):
    """A 502 means the message most likely never arrived: the retry must relay for real."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    token = (await _patient(pclient))["access_token"]

    _mesh(monkeypatch, lambda r: httpx.Response(500, json={"detail": "x"}, request=r))
    assert (await _send(client, token, KEY)).status_code == 502

    calls = _mesh(monkeypatch, _turn)
    retry = await _send(client, token, KEY)
    assert retry.status_code == 200, retry.text
    assert len(_inbound(calls)) == 1
    assert idempotency.REPLAYED_HEADER not in retry.headers


async def test_a_repeated_upload_is_relayed_once(pclient, monkeypatch):
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _turn)
    token = (await _patient(pclient))["access_token"]
    headers = {**_bearer(token), idempotency.HEADER: KEY}
    image = _png()

    for _ in range(2):
        resp = await client.post(
            PRECHECK, headers=headers, files={"file": ("exame.png", image, "image/png")}
        )
        assert resp.status_code == 200, resp.text
    assert len(_inbound(calls)) == 1


async def test_a_key_is_scoped_to_one_patient_and_one_thread(pclient, monkeypatch):
    """The same key from another patient, or on the other product's thread, is its own message."""
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _turn)
    ana = (await _patient(pclient, "ana@exemplo.com"))["access_token"]

    assert (await _send(client, ana, KEY)).status_code == 200
    assert (await _send(client, ana, KEY, url=SECRETARIA)).status_code == 200
    # A second login in the same client replaces the cookie jar's session; the bearer decides.
    bia = (await _patient(pclient, "bia@exemplo.com"))["access_token"]
    assert (await _send(client, bia, KEY)).status_code == 200
    assert len(_inbound(calls)) == 3


@pytest.mark.parametrize("bad", ["", "curta", "a" * 129, "../../etc/passwd", "id com espaco"])
async def test_a_malformed_key_is_refused_before_any_relay(pclient, monkeypatch, bad):
    client, _, _ = pclient
    _configure_mesh(monkeypatch)
    calls = _mesh(monkeypatch, _turn)
    token = (await _patient(pclient))["access_token"]

    resp = await _send(client, token, bad)
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": "invalid_idempotency_key"}
    assert _inbound(calls) == []


# --- 2) The cache on its own -------------------------------------------------------------------


async def test_a_retry_during_the_first_attempt_waits_for_it():
    """The client timed out, the server did not: the retry gets the first attempt's answer."""
    cache = idempotency.IdempotencyCache()
    sent = 0
    gate = asyncio.Event()

    async def send():
        nonlocal sent
        sent += 1
        await gate.wait()
        return {"n": sent}

    first = asyncio.create_task(cache.run("p:precheck", KEY, send))
    await asyncio.sleep(0)
    second = asyncio.create_task(cache.run("p:precheck", KEY, send))
    await asyncio.sleep(0)
    gate.set()

    assert await first == ({"n": 1}, False)
    assert await second == ({"n": 1}, True)
    assert sent == 1


async def test_a_waiter_takes_over_when_the_first_attempt_is_cancelled():
    cache = idempotency.IdempotencyCache()
    calls = 0

    async def slow():
        nonlocal calls
        calls += 1
        await asyncio.sleep(10)

    async def fast():
        nonlocal calls
        calls += 1
        return "ok"

    first = asyncio.create_task(cache.run("p:precheck", KEY, slow))
    await asyncio.sleep(0)
    second = asyncio.create_task(cache.run("p:precheck", KEY, fast))
    await asyncio.sleep(0)
    first.cancel()

    assert await second == ("ok", False)
    assert calls == 2
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_an_expired_key_relays_again_and_the_size_is_bounded():
    cache = idempotency.IdempotencyCache(ttl=0, max_entries=3)
    calls = 0

    async def send():
        nonlocal calls
        calls += 1
        return calls

    assert await cache.run("s", KEY, send) == (1, False)
    assert await cache.run("s", KEY, send) == (2, False)  # ttl 0: already forgotten

    bounded = idempotency.IdempotencyCache(ttl=600, max_entries=3)
    for i in range(10):
        await bounded.run("s", f"key-{i:04d}", send)
    assert len(bounded._entries) <= 3
