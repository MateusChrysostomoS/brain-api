"""First access is an isolated credential, never an operational staff session."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from brain_api.core.database import get_session
from brain_api.core.security import decode_token, hash_password
from brain_api.main import create_app
from brain_api.models import Tenant, User


@pytest_asyncio.fixture
async def pending_client(db_session):
    tenant = Tenant(clinic_name="Test Clinic")
    db_session.add(tenant)
    await db_session.flush()
    user = User(
        tenant_id=tenant.id,
        email="staff@example.com",
        name="Staff",
        role="doctor",
        password_hash=hash_password("TemporaryTest7"),
    )
    user.requires_password_change = True
    user.temporary_password_expires_at = datetime.now(UTC) + timedelta(hours=72)
    db_session.add(user)
    await db_session.commit()
    app = create_app()

    async def get_test_session():
        yield db_session

    app.dependency_overrides[get_session] = get_test_session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://test.example.com"
    ) as c:
        yield c, user, db_session


async def temp_login(c):
    return await c.post(
        "/auth/token", json={"email": "staff@example.com", "password": "TemporaryTest7"}
    )


async def test_temporary_login_is_restricted_until_password_change(pending_client):
    c, user, db = pending_client
    response = await temp_login(c)
    assert response.status_code == 200
    assert response.json().get("requires_password_change") is True
    restricted = response.json()["access_token"]
    assert decode_token(restricted)["scope"] == "staff_password_setup"
    assert not response.json().get("refresh_token")
    assert c.cookies.get("__Host-staff_setup")
    assert not c.cookies.get("__Host-refresh_token")
    for path in ("/auth/me", "/doctor/professionals", "/entitlements"):
        assert (
            await c.get(path, headers={"Authorization": f"Bearer {restricted}"})
        ).status_code == 401
    for path in ("/doctor/secretaria/hub-token", "/sso/precheck/token"):
        assert (
            await c.post(path, headers={"Authorization": f"Bearer {restricted}"})
        ).status_code == 401
    complete = await c.post(
        "/auth/first-access/complete",
        headers={"Authorization": f"Bearer {restricted}"},
        json={"new_password": "PersonalTest9"},
    )
    assert complete.status_code == 200
    assert complete.json()["requires_password_change"] is False
    assert not c.cookies.get("__Host-staff_setup")
    assert c.cookies.get("__Host-refresh_token")
    assert (await temp_login(c)).status_code == 401
    assert (
        await c.post("/auth/token", json={"email": user.email, "password": "PersonalTest9"})
    ).status_code == 200
    replay = await c.post(
        "/auth/first-access/complete",
        headers={"Authorization": f"Bearer {restricted}"},
        json={"new_password": "AnotherTest8"},
    )
    assert replay.status_code == 401


async def test_resume_requires_csrf_and_keeps_restricted_scope(pending_client):
    c, user, db = pending_client
    await temp_login(c)
    assert (await c.post("/auth/first-access/resume")).status_code == 403
    resumed = await c.post("/auth/first-access/resume", headers={"X-Brain-Client": "web"})
    assert resumed.status_code == 200
    assert decode_token(resumed.json()["access_token"])["scope"] == "staff_password_setup"


async def test_resume_without_setup_cookie_is_not_an_authentication_failure(pending_client):
    c, _, _ = pending_client
    response = await c.post("/auth/first-access/resume", headers={"X-Brain-Client": "web"})
    assert response.status_code == 200
    assert response.json() == {"requires_password_change": False}
    assert "set-cookie" not in response.headers
    assert (await c.post("/auth/first-access/resume")).status_code == 403


async def test_resume_with_invalid_setup_cookie_still_rejects(pending_client):
    c, _, _ = pending_client
    c.cookies.set("__Host-staff_setup", "invalid", domain="test.example.com", path="/")
    response = await c.post("/auth/first-access/resume", headers={"X-Brain-Client": "web"})
    assert response.status_code == 401
    assert not c.cookies.get("__Host-staff_setup")


async def test_expired_temporary_password_cannot_login(pending_client):
    c, user, db = pending_client
    user.temporary_password_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()
    assert (await temp_login(c)).status_code == 401


async def test_same_password_and_new_generation_refuse_completion(pending_client):
    c, user, db = pending_client
    response = await temp_login(c)
    token = response.json()["access_token"]
    same = await c.post(
        "/auth/first-access/complete",
        headers={"Authorization": f"Bearer {token}"},
        json={"new_password": "TemporaryTest7"},
    )
    assert same.status_code == 422
    user.credential_version += 1
    await db.commit()
    stale = await c.post(
        "/auth/first-access/complete",
        headers={"Authorization": f"Bearer {token}"},
        json={"new_password": "PersonalTest9"},
    )
    assert stale.status_code == 401


async def test_first_access_rejects_password_over_bcrypt_byte_limit(pending_client):
    c, user, db = pending_client
    token = (await temp_login(c)).json()["access_token"]
    response = await c.post(
        "/auth/first-access/complete",
        headers={"Authorization": f"Bearer {token}"},
        json={"new_password": "é" * 40 + "9"},
    )
    assert response.status_code == 422


async def test_recovery_concludes_setup_and_burns_legacy_invite(pending_client):
    from brain_api.core.security import hash_refresh_token
    from brain_api.services.auth import issue_password_reset_token

    c, user, db = pending_client
    user.invite_token_hash = hash_refresh_token("legacy-fixture-token")
    user.invite_token_expires_at = datetime.now(UTC) + timedelta(hours=72)
    await db.commit()
    _, reset = await issue_password_reset_token(db, user.email)
    response = await c.post(
        "/auth/password-reset/confirm", json={"token": reset, "new_password": "PersonalTest9"}
    )
    assert response.status_code == 200
    await db.refresh(user)
    assert user.requires_password_change is False
    assert user.invite_token_hash is None
    assert user.invite_token_expires_at is None
    assert user.credential_version == 1
    old_invite = await c.post("/auth/exchange-invite-token", json={"token": "legacy-fixture-token"})
    assert old_invite.status_code == 401


@pytest.mark.parametrize("password", ["TemporaryTest7", "é" * 40 + "9"])
async def test_recovery_obeys_first_access_password_policy(pending_client, password):
    from brain_api.services.auth import issue_password_reset_token

    c, user, db = pending_client
    _, reset = await issue_password_reset_token(db, user.email)
    response = await c.post(
        "/auth/password-reset/confirm", json={"token": reset, "new_password": password}
    )
    assert response.status_code == 422


async def test_temporary_login_clears_previous_staff_cookie_and_logout_clears_setup(pending_client):
    c, user, db = pending_client
    c.cookies.set(
        "__Host-refresh_token", "previous-test-session", domain="test.example.com", path="/"
    )
    await temp_login(c)
    assert not c.cookies.get("__Host-refresh_token")
    await c.post("/auth/logout")
    assert not c.cookies.get("__Host-staff_setup")
