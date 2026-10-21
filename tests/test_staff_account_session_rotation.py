"""TASK-065: the personal staff session is renewed IN PLACE, like the patient's.

Production symptom (2026-10-20): the portal open in two tabs. Every `POST /auth/account/resume`
revoked the `StaffAccountSession` row and opened a new one, so every clinical token bound to
the old row by `account_sid` died (the other tab got 401, resumed, and killed the first one),
and two resumes in flight with one cookie read as theft and signed the whole account out.

Contract under test (mirrors `services/portal/patient_access.py::rotate_patient_session`):
- the row keeps its id across resumes; only the opaque cookie value rotates;
- the value a resume just replaced is accepted for
  `STAFF_ACCOUNT_SESSION_ROTATION_GRACE_SECONDS` (answer without Set-Cookie);
- after that window it is the reuse signal: the whole account ends, as before;
- logout, revocation, reset and disable still kill every token immediately.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from brain_api.api.deps import get_current_principal, get_token_principal
from brain_api.config import get_settings
from brain_api.core.security import decode_token
from brain_api.services import staff_accounts as service
from tests.test_rbac import OWNER_A_EMAIL, OWNER_A_PASSWORD
from tests.test_staff_account_rules import seed

pytestmark = pytest.mark.usefixtures("staff_accounts_enabled")

COOKIE = "__Host-staff_account"
WEB = {"X-Brain-Client": "web"}
LOGIN = {"email": OWNER_A_EMAIL, "password": OWNER_A_PASSWORD}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _grace() -> int:
    return getattr(get_settings(), "STAFF_ACCOUNT_SESSION_ROTATION_GRACE_SECONDS", 60)


def _travel(monkeypatch, seconds: int) -> None:
    """Move the session service's clock forward (both base and fixed code read it there)."""
    real = service.datetime

    class Shifted(real):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + timedelta(seconds=seconds)

    monkeypatch.setattr(service, "datetime", Shifted)


async def _login_and_select(client):
    client.base_url = "https://test"
    login = await client.post("/auth/account/login", json=LOGIN)
    assert login.status_code == 200
    body = login.json()
    selected = await client.post(
        "/auth/account/select",
        headers=_bearer(body["account_token"]),
        json={"user_id": body["memberships"][0]["user_id"]},
    )
    assert selected.status_code == 200
    return body, selected.json()


async def _resume(client, cookie: str | None = None):
    headers = dict(WEB)
    if cookie is not None:
        headers["Cookie"] = f"{COOKIE}={cookie}"
    return await client.post("/auth/account/resume", headers=headers)


async def _me(client, token: str) -> int:
    return (await client.get("/auth/me", headers=_bearer(token))).status_code


async def _select(client, account_token: str, user_id: str):
    return await client.post(
        "/auth/account/select", headers=_bearer(account_token), json={"user_id": user_id}
    )


# --- (a) a clinical token outlives a later resume ------------------------------------------


async def test_clinical_token_stays_valid_after_a_later_resume(client):
    body, clinical = await _login_and_select(client)
    resumed = await _resume(client)
    assert resumed.status_code == 200
    assert await _me(client, clinical["access_token"]) == 200
    again = await _select(
        client, resumed.json()["account_token"], body["memberships"][0]["user_id"]
    )
    assert again.status_code == 200
    # Same row: the new clinical token names the same account session as the old one.
    assert (
        decode_token(again.json()["access_token"])["account_sid"]
        == decode_token(clinical["access_token"])["account_sid"]
    )


async def test_two_tabs_resuming_in_turn_never_kill_each_other(client):
    """The production ping-pong: tab A works, tab B resumes and selects, then the reverse."""
    body, tab_a = await _login_and_select(client)
    user_id = body["memberships"][0]["user_id"]
    for _ in range(3):
        resumed = await _resume(client)
        assert resumed.status_code == 200
        tab_b = (await _select(client, resumed.json()["account_token"], user_id)).json()
        assert await _me(client, tab_a["access_token"]) == 200
        assert await _me(client, tab_b["access_token"]) == 200
        tab_a = tab_b


# --- (b) two resumes with the same cookie inside the grace window --------------------------


async def test_two_resumes_with_the_same_cookie_keep_the_account_alive(client):
    body, clinical = await _login_and_select(client)
    user_id = body["memberships"][0]["user_id"]
    t0 = client.cookies.get(COOKIE)
    client.cookies.clear()

    first = await _resume(client, cookie=t0)
    assert first.status_code == 200
    t1 = first.cookies.get(COOKIE)
    assert t1 and t1 != t0
    client.cookies.clear()

    second = await _resume(client, cookie=t0)
    assert second.status_code == 200, second.text
    # The browser already holds the successor from the first answer: no competing cookie.
    assert "set-cookie" not in second.headers
    # The account is alive: selection works, old and new clinical tokens open the clinic.
    selected = await _select(client, second.json()["account_token"], user_id)
    assert selected.status_code == 200
    assert await _me(client, selected.json()["access_token"]) == 200
    assert await _me(client, clinical["access_token"]) == 200
    # And the successor still renews.
    assert (await _resume(client, cookie=t1)).status_code == 200


async def test_two_parallel_resumes_with_the_same_cookie_both_succeed(client):
    body, clinical = await _login_and_select(client)
    t0 = client.cookies.get(COOKIE)
    client.cookies.clear()

    a, b = await asyncio.gather(_resume(client, cookie=t0), _resume(client, cookie=t0))
    assert (a.status_code, b.status_code) == (200, 200), (a.text, b.text)
    issued = [c for c in (a.cookies.get(COOKIE), b.cookies.get(COOKIE)) if c]
    assert len(issued) == 1, "exactly one of them rotated; the other landed in the grace branch"
    assert await _me(client, clinical["access_token"]) == 200
    client.cookies.clear()
    assert (await _resume(client, cookie=issued[0])).status_code == 200


# --- (c) the replaced value after the grace window is theft, as before ---------------------


async def test_previous_cookie_after_the_grace_window_ends_the_account(client, monkeypatch):
    body, clinical = await _login_and_select(client)
    t0 = client.cookies.get(COOKIE)
    resumed = await _resume(client)
    assert resumed.status_code == 200
    t1 = client.cookies.get(COOKIE)
    assert t1 and t1 != t0

    _travel(monkeypatch, _grace() + 5)
    client.cookies.clear()
    replay = await _resume(client, cookie=t0)
    assert replay.status_code == 401
    assert "max-age=0" in replay.headers["set-cookie"].lower()
    # Every token of the account is dead, and so is the successor cookie.
    assert await _me(client, clinical["access_token"]) == 401
    proof = _bearer(resumed.json()["account_token"])
    assert (await client.get("/auth/account/memberships", headers=proof)).status_code == 401
    assert (await _resume(client, cookie=t1)).status_code == 401


# --- (d) logout still kills clinical tokens immediately ------------------------------------


async def test_cookie_logout_after_a_resume_kills_earlier_clinical_tokens(client):
    _, clinical = await _login_and_select(client)
    assert (await _resume(client)).status_code == 200
    assert (await client.post("/auth/account/logout")).status_code == 204
    assert await _me(client, clinical["access_token"]) == 401
    assert (await _resume(client)).status_code == 401


async def test_logout_with_the_just_replaced_cookie_still_ends_the_account(client):
    """A tab that logs out carrying the value a parallel resume just replaced must still
    sign the person out — logout can only take access away."""
    _, clinical = await _login_and_select(client)
    t0 = client.cookies.get(COOKIE)
    assert (await _resume(client)).status_code == 200
    t1 = client.cookies.get(COOKIE)
    client.cookies.clear()
    logout = await client.post("/auth/account/logout", headers={"Cookie": f"{COOKIE}={t0}"})
    assert logout.status_code == 204
    assert await _me(client, clinical["access_token"]) == 401
    assert (await _resume(client, cookie=t1)).status_code == 401


# --- service level: stable id, sliding expiry, kill switches after a rotation --------------


def _clinical(user, row) -> str:
    from brain_api.api.auth import build_session_response

    return build_session_response(user, None, account_session_id=str(row.id)).access_token


async def _principal(db_session, token):
    return await get_current_principal(get_token_principal(f"Bearer {token}"), db_session)


async def test_rotation_keeps_the_row_id_and_slides_expiry(db_session):
    account, a, _, _ = await seed(db_session)
    row, raw = await service.open_account_session(db_session, account)
    row_id = row.id
    row.expires_at = datetime.now(UTC) + timedelta(days=1)
    await db_session.commit()
    token = _clinical(a, row)

    _, rotated, new_raw = await service.rotate_account_session(db_session, raw)
    assert rotated.id == row_id
    assert new_raw and new_raw != raw
    expected = datetime.now(UTC) + timedelta(days=get_settings().REFRESH_TOKEN_EXPIRE_DAYS)
    assert abs((service.aware(rotated.expires_at) - expected).total_seconds()) < 60
    assert (await _principal(db_session, token)).user_id == str(a.id)


async def test_a_value_two_rotations_old_ends_the_account(db_session):
    """Review H1: a thief who resumes twice (t0→t1→t2) must not shake off reuse detection.
    The victim's t0 names the row, matches neither current nor previous → account ends."""
    account, a, _, _ = await seed(db_session)
    row, t0 = await service.open_account_session(db_session, account)
    token = _clinical(a, row)
    _, _, t1 = await service.rotate_account_session(db_session, t0)
    _, row, t2 = await service.rotate_account_session(db_session, t1)
    with pytest.raises(HTTPException):
        await service.rotate_account_session(db_session, t0)
    with pytest.raises(HTTPException):
        await service.live_session(db_session, row.id)
    with pytest.raises(HTTPException):
        await _principal(db_session, token)
    with pytest.raises(HTTPException):
        await service.rotate_account_session(db_session, t2)


async def test_thief_double_resume_then_victim_resume_signs_everyone_out(client):
    _, clinical = await _login_and_select(client)
    victim = client.cookies.get(COOKIE)
    client.cookies.clear()
    stolen = await _resume(client, cookie=victim)
    thief = stolen.cookies.get(COOKIE)
    client.cookies.clear()
    again = await _resume(client, cookie=thief)
    assert again.status_code == 200
    thief = again.cookies.get(COOKIE)
    client.cookies.clear()

    assert (await _resume(client, cookie=victim)).status_code == 401
    assert await _me(client, clinical["access_token"]) == 401
    assert (await _resume(client, cookie=thief)).status_code == 401


async def test_cookie_names_its_row(client):
    body, _ = await _login_and_select(client)
    sid = service.decode_account_token(body["account_token"])["sid"]
    cookie = client.cookies.get(COOKIE)
    assert cookie.startswith(f"{sid}.")
    resumed = await _resume(client)
    assert resumed.cookies.get(COOKIE).startswith(f"{sid}.")
    assert resumed.cookies.get(COOKIE) != cookie


async def test_new_format_cookie_with_a_wrong_secret_ends_the_account(db_session):
    account, a, _, _ = await seed(db_session)
    row, raw = await service.open_account_session(db_session, account)
    token = _clinical(a, row)
    with pytest.raises(HTTPException):
        await service.rotate_account_session(db_session, f"{row.id}.garbage")
    with pytest.raises(HTTPException):
        await _principal(db_session, token)
    with pytest.raises(HTTPException):
        await service.rotate_account_session(db_session, raw)


@pytest.mark.parametrize(
    "shape",
    ["not-a-uuid.secret", "{sid}.", ".secret", "{sid}.a.b", "{other}.secret", "", "garbage"],
)
async def test_malformed_or_unknown_cookie_is_a_plain_401(db_session, shape):
    """A value that cannot name a real row ends nothing (it cannot be attributed)."""
    from uuid import uuid4

    account, a, _, _ = await seed(db_session)
    row, raw = await service.open_account_session(db_session, account)
    token = _clinical(a, row)
    with pytest.raises(HTTPException):
        await service.rotate_account_session(db_session, shape.format(sid=row.id, other=uuid4()))
    assert (await _principal(db_session, token)).user_id == str(a.id)
    assert (await service.rotate_account_session(db_session, raw))[2]


async def _legacy_session(db_session, account, legacy: str):
    """A row as written before TASK-065: the cookie is the bare secret, no row id."""
    from uuid import uuid4

    from brain_api.core.security import hash_refresh_token
    from brain_api.models import StaffAccountSession

    row = StaffAccountSession(
        id=uuid4(),
        account_id=account.id,
        token_hash=hash_refresh_token(legacy),
        credential_version=account.credential_version,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def test_legacy_cookie_resumes_and_comes_back_in_the_new_format(db_session, monkeypatch):
    from brain_api.core.security import generate_refresh_token

    account, a, _, _ = await seed(db_session)
    legacy = generate_refresh_token()
    row = await _legacy_session(db_session, account, legacy)
    token = _clinical(a, row)

    _, rotated, new_raw = await service.rotate_account_session(db_session, legacy)
    assert rotated.id == row.id
    assert service.parse_cookie_value(new_raw) == (row.id, new_raw.split(".", 1)[1])
    assert (await _principal(db_session, token)).user_id == str(a.id)
    # The legacy value is now the previous one: grace inside the window...
    assert (await service.rotate_account_session(db_session, legacy))[2] is None
    # ...and reuse after it.
    monkeypatch.setattr(get_settings(), "STAFF_ACCOUNT_SESSION_ROTATION_GRACE_SECONDS", 0)
    with pytest.raises(HTTPException):
        await service.rotate_account_session(db_session, legacy)
    with pytest.raises(HTTPException):
        await _principal(db_session, token)


async def test_legacy_cookie_resumes_over_http_and_gets_the_new_format(client):
    from uuid import UUID

    from brain_api.core.database import get_session
    from brain_api.core.security import generate_refresh_token, hash_refresh_token
    from brain_api.main import app
    from brain_api.models import StaffAccountSession

    body, clinical = await _login_and_select(client)
    sid = service.decode_account_token(body["account_token"])["sid"]
    legacy = generate_refresh_token()
    # Rewrite this sign-in as a pre-TASK-065 row: same id, bare-secret hash.
    async for session in app.dependency_overrides[get_session]():
        row = await session.get(StaffAccountSession, UUID(sid))
        row.token_hash = hash_refresh_token(legacy)
        await session.commit()
    client.cookies.clear()
    resumed = await _resume(client, cookie=legacy)
    assert resumed.status_code == 200
    assert resumed.cookies.get(COOKIE).startswith(f"{sid}.")
    assert await _me(client, clinical["access_token"]) == 200


async def test_logout_ignores_a_cookie_it_cannot_prove(client):
    """Logout parses like resume; a wrong secret or malformed value ends nothing."""
    from uuid import uuid4

    body, clinical = await _login_and_select(client)
    sid = service.decode_account_token(body["account_token"])["sid"]
    good = client.cookies.get(COOKIE)
    client.cookies.clear()
    for bad in (f"{sid}.garbage", "not-a-uuid.x", f"{uuid4()}.x", "garbage"):
        out = await client.post("/auth/account/logout", headers={"Cookie": f"{COOKIE}={bad}"})
        assert out.status_code == 204
        assert await _me(client, clinical["access_token"]) == 200
    out = await client.post("/auth/account/logout", headers={"Cookie": f"{COOKIE}={good}"})
    assert out.status_code == 204
    assert await _me(client, clinical["access_token"]) == 401


async def test_reuse_and_grace_log_session_ids_only(db_session, capsys, monkeypatch):
    account, _, _, _ = await seed(db_session)
    row, t0 = await service.open_account_session(db_session, account)
    _, _, t1 = await service.rotate_account_session(db_session, t0)
    capsys.readouterr()
    assert (await service.rotate_account_session(db_session, t0))[2] is None
    monkeypatch.setattr(get_settings(), "STAFF_ACCOUNT_SESSION_ROTATION_GRACE_SECONDS", 0)
    with pytest.raises(HTTPException):
        await service.rotate_account_session(db_session, t0)
    out = capsys.readouterr().out
    assert "staff_account_session_grace_accepted" in out
    assert "staff_account_session_reuse_detected" in out
    assert str(row.id) in out
    for secret in (t0, t1, t0.split(".", 1)[1], t1.split(".", 1)[1]):
        assert secret not in out
    assert account.email not in out and str(account.id) not in out


async def test_password_reset_after_rotation_kills_tokens_and_cookie(db_session):
    from brain_api.services.auth import (
        complete_password_reset,
        issue_password_reset_code,
        verify_password_reset_code,
    )

    account, a, _, _ = await seed(db_session)
    row, raw = await service.open_account_session(db_session, account)
    _, row, raw = await service.rotate_account_session(db_session, raw)
    token = _clinical(a, row)
    _, code = await issue_password_reset_code(db_session, account.email)
    reset = await verify_password_reset_code(db_session, account.email, code)
    assert await complete_password_reset(db_session, reset, "NewPassword123")
    with pytest.raises(HTTPException):
        await _principal(db_session, token)
    with pytest.raises(HTTPException):
        await service.rotate_account_session(db_session, raw)


async def test_membership_revocation_after_rotation_kills_that_clinic(db_session):
    account, a, b, _ = await seed(db_session)
    row, raw = await service.open_account_session(db_session, account)
    _, row, raw = await service.rotate_account_session(db_session, raw)
    token_a, token_b = _clinical(a, row), _clinical(b, row)
    a.access_revoked_at = datetime.now(UTC)
    a.credential_version += 1
    await db_session.commit()
    with pytest.raises(HTTPException):
        await _principal(db_session, token_a)
    assert (await _principal(db_session, token_b)).user_id == str(b.id)


async def test_disabled_account_after_rotation_kills_tokens_and_cookie(db_session):
    account, a, _, _ = await seed(db_session)
    row, raw = await service.open_account_session(db_session, account)
    _, row, raw = await service.rotate_account_session(db_session, raw)
    token = _clinical(a, row)
    account.disabled_at = datetime.now(UTC)
    await db_session.commit()
    with pytest.raises(HTTPException):
        await _principal(db_session, token)
    with pytest.raises(HTTPException):
        await service.rotate_account_session(db_session, raw)


async def test_credential_version_bump_after_rotation_kills_grace_too(db_session):
    """A kill switch inside the grace window: the replaced value must not resurrect it."""
    account, a, _, _ = await seed(db_session)
    row, t0 = await service.open_account_session(db_session, account)
    _, row, _ = await service.rotate_account_session(db_session, t0)
    token = _clinical(a, row)
    await service.end_account_sessions(db_session, account.id)
    await db_session.commit()
    with pytest.raises(HTTPException):
        await _principal(db_session, token)
    with pytest.raises(HTTPException):
        await service.rotate_account_session(db_session, t0)
