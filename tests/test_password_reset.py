"""Staff password reset by e-mailed code — the HTTP surface (TASK-056).

Ground truth: api/auth.py (`/auth/password-reset/*`), services/auth.py,
services/mailer.py (`password_reset_code`). History: the link-based flow of 2026-08-14
(docs/CHECKPOINT_password_reset.md) was replaced by a 6-digit code.
"""

import pytest
from structlog.testing import capture_logs

import brain_api.api.auth as auth_api
from brain_api.core.ratelimit import SlidingWindowLimiter
from tests.test_rbac import OWNER_A_EMAIL, OWNER_A_PASSWORD

NEW_PASSWORD = "novaSenha123"
GENERIC = "Se houver uma conta com esse e-mail, enviamos um código para ele."


def _code(mail_spy) -> str:
    return mail_spy.of("password_reset_code")[-1][2]["code"]


async def _token(client, mail_spy) -> str:
    await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    r = await client.post(
        "/auth/password-reset/verify-code", json={"email": OWNER_A_EMAIL, "code": _code(mail_spy)}
    )
    assert r.status_code == 200
    return r.json()["reset_token"]


async def test_end_to_end_code_flow(client, mail_spy):
    r = await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    assert r.status_code == 200 and r.json() == {"detail": GENERIC}
    assert r.headers["cache-control"] == "no-store"
    to, template, variables = mail_spy.last
    assert to == OWNER_A_EMAIL and template == "password_reset_code"
    assert set(variables) == {"name", "code", "ttl_minutes"}

    r = await client.post(
        "/auth/password-reset/verify-code", json={"email": OWNER_A_EMAIL, "code": _code(mail_spy)}
    )
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    token = r.json()["reset_token"]

    r = await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"

    old = await client.post(
        "/auth/token", json={"email": OWNER_A_EMAIL, "password": OWNER_A_PASSWORD}
    )
    new = await client.post("/auth/token", json={"email": OWNER_A_EMAIL, "password": NEW_PASSWORD})
    assert (old.status_code, new.status_code) == (401, 200)


async def test_request_is_identical_for_unknown_address_and_sends_nothing(client, mail_spy):
    known = await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    mail_spy.calls.clear()
    unknown = await client.post(
        "/auth/password-reset/request", json={"email": "ninguem@example.com"}
    )
    assert (unknown.status_code, unknown.json()) == (known.status_code, known.json())
    assert unknown.headers.get("cache-control") == known.headers.get("cache-control")
    assert mail_spy.calls == []


async def test_wrong_code_and_unknown_email_get_the_same_400(client, mail_spy):
    await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    code = _code(mail_spy)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    a = await client.post(
        "/auth/password-reset/verify-code", json={"email": OWNER_A_EMAIL, "code": wrong}
    )
    b = await client.post(
        "/auth/password-reset/verify-code", json={"email": "ninguem@example.com", "code": code}
    )
    assert a.status_code == b.status_code == 400
    assert a.json() == b.json() == {"detail": "Código inválido ou expirado"}


@pytest.mark.parametrize("code", ["12345", "1234567", "12a456", " 123456", "١٢٣٤٥٦"])
async def test_malformed_code_is_422(client, code):
    r = await client.post(
        "/auth/password-reset/verify-code", json={"email": OWNER_A_EMAIL, "code": code}
    )
    assert r.status_code == 422


async def test_link_preflight_route_is_gone(client):
    r = await client.post("/auth/password-reset/verify", json={"token": "x" * 43})
    assert r.status_code in (404, 405)


async def test_confirm_revokes_existing_sessions(client, mail_spy):
    login = await client.post(
        "/auth/token", json={"email": OWNER_A_EMAIL, "password": OWNER_A_PASSWORD}
    )
    refresh = login.json()["refresh_token"]
    token = await _token(client, mail_spy)
    await client.post(
        "/auth/password-reset/confirm", json={"token": token, "new_password": NEW_PASSWORD}
    )
    # The session opened before the reset can no longer be renewed.
    r = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 401


async def test_per_address_request_limit(client, monkeypatch):
    monkeypatch.setattr(
        auth_api, "_reset_email_limiter", SlidingWindowLimiter("reset_email_test", lambda: 1)
    )
    first = await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    second = await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
    other = await client.post("/auth/password-reset/request", json={"email": "x@example.com"})
    assert (first.status_code, second.status_code, other.status_code) == (200, 429, 200)


async def test_logs_never_carry_email_or_code(client, mail_spy):
    with capture_logs() as logs:
        await client.post("/auth/password-reset/request", json={"email": OWNER_A_EMAIL})
        code = _code(mail_spy)
        await client.post(
            "/auth/password-reset/verify-code", json={"email": OWNER_A_EMAIL, "code": code}
        )
    flat = repr(logs)
    assert OWNER_A_EMAIL not in flat and code not in flat
