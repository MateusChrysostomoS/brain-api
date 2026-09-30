"""`return_to` on POST /billing/checkout: allowlist + server-built success_url.

The Brain-Message console sends a clinic to the brain-frontend plans page; after paying,
the buyer must land back in the console. The success URL is built HERE from the
configured base (never from anything the client sends), so there is no open redirect.
"""

import pytest
from fastapi import HTTPException

from brain_api.services.billing import (
    RETURN_TO_ALLOWLIST,
    success_url_for,
    validate_return_to,
)
from tests.test_billing import _install_fake_stripe_httpx
from tests.test_rbac import OWNER_B_EMAIL, OWNER_B_PASSWORD, _bearer, _token

BASE = "https://brain.example.com/checkout/sucesso?session_id={CHECKOUT_SESSION_ID}"
FAKE_URL = "https://checkout.stripe.test/session"
CONFIGURED_SUCCESS = "http://localhost:3000/app?checkout=success"


# --- Pure helpers ------------------------------------------------------------------


def test_allowlist_is_exactly_console():
    assert RETURN_TO_ALLOWLIST == frozenset({"console"})


def test_validate_return_to_accepts_none_and_console():
    assert validate_return_to(None) is None
    assert validate_return_to("console") == "console"


@pytest.mark.parametrize(
    "hostile",
    [
        "",
        "Console",
        "console ",
        " console",
        "console&x=1",
        "console#x",
        "https://evil.com",
        "//evil.com",
        "/anamneses/",
        "portal",
        "console" * 50,
    ],
)
def test_validate_return_to_rejects_everything_else(hostile):
    with pytest.raises(HTTPException) as exc:
        validate_return_to(hostile)
    assert exc.value.status_code == 422
    assert exc.value.detail == "unknown_return_to"


def test_success_url_is_untouched_without_return_to():
    assert success_url_for(BASE, None, carries_precheck=True) == BASE


def test_success_url_appends_origem_and_keeps_the_stripe_template():
    assert success_url_for(BASE, "console", carries_precheck=False) == BASE + "&origem=console"
    assert (
        success_url_for(BASE, "console", carries_precheck=True)
        == BASE + "&origem=console&produto=precheck"
    )


def test_success_url_without_a_query_string_starts_one():
    assert (
        success_url_for("https://b.example.com/checkout/sucesso", "console", carries_precheck=False)
        == "https://b.example.com/checkout/sucesso?origem=console"
    )


def test_success_url_goes_before_a_fragment():
    assert (
        success_url_for("https://b.example.com/x#f", "console", carries_precheck=True)
        == "https://b.example.com/x?origem=console&produto=precheck#f"
    )


def test_success_url_helper_refuses_a_value_outside_the_allowlist():
    with pytest.raises(HTTPException) as exc:
        success_url_for(BASE, "https://evil.com", carries_precheck=False)
    assert exc.value.status_code == 422


# --- Endpoint ----------------------------------------------------------------------


async def _checkout(client, plan: str, **extra):
    token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    return await client.post(
        "/billing/checkout", headers=_bearer(token), json={"plan": plan, **extra}
    )


async def test_return_to_console_appends_origem_and_produto_for_a_precheck_plan(
    client, monkeypatch
):
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})

    resp = await _checkout(client, "precheck_basic", return_to="console")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": FAKE_URL}
    assert (
        captured["data"]["success_url"] == CONFIGURED_SUCCESS + "&origem=console&produto=precheck"
    )
    assert captured["data"]["cancel_url"] == "http://localhost:3000/app?checkout=cancelled"


async def test_return_to_console_on_a_plan_without_precheck_has_no_produto(client, monkeypatch):
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})

    resp = await _checkout(client, "secretaria_basico", return_to="console")

    assert resp.status_code == 200, resp.text
    assert captured["data"]["success_url"] == CONFIGURED_SUCCESS + "&origem=console"


async def test_without_return_to_the_success_url_is_exactly_the_configured_one(client, monkeypatch):
    """Direction 2: old callers (no return_to) are byte-for-byte unchanged."""
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})

    resp = await _checkout(client, "precheck_basic")

    assert resp.status_code == 200, resp.text
    assert captured["data"]["success_url"] == CONFIGURED_SUCCESS


@pytest.mark.parametrize(
    "hostile",
    ["https://evil.com", "//evil.com", "Console", "console&x=1", "console" * 50, ""],
)
async def test_hostile_return_to_is_422_and_never_reaches_stripe(client, monkeypatch, hostile):
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})

    resp = await _checkout(client, "precheck_basic", return_to=hostile)

    assert resp.status_code == 422, resp.text
    assert captured == {}, "Stripe must never be reached"


async def test_the_client_can_never_supply_a_success_url(client, monkeypatch):
    """No open redirect: the schema is extra=forbid, so a client URL is a 422."""
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})

    resp = await _checkout(
        client, "precheck_basic", return_to="console", success_url="https://evil.com/x"
    )

    assert resp.status_code == 422, resp.text
    assert captured == {}
