"""Existing bearer/refresh/hub credentials cannot survive revocation or reissue."""

from datetime import UTC, datetime

from brain_api.core.security import create_hub_token
from brain_api.models import Entitlement
from tests.test_staff_first_access import pending_client, temp_login  # noqa: F401


async def activate(c):
    setup = (await temp_login(c)).json()["access_token"]
    response = await c.post(
        "/auth/first-access/complete",
        json={"new_password": "PersonalTest9"},
        headers={"Authorization": f"Bearer {setup}"},
    )
    assert response.status_code == 200
    return response.json()


async def test_revocation_refuses_existing_access_and_refresh(pending_client):
    c, user, db = pending_client
    login = await activate(c)
    headers = {"Authorization": f"Bearer {login['access_token']}"}
    assert (await c.get("/auth/me", headers=headers)).status_code == 200
    user.access_revoked_at = datetime.now(UTC)
    await db.commit()
    assert (await c.get("/auth/me", headers=headers)).status_code == 401
    assert (await c.post("/auth/refresh", headers={"X-Brain-Client": "web"})).status_code == 401


async def test_new_version_invalidates_existing_access(pending_client):
    c, user, db = pending_client
    login = await activate(c)
    user.credential_version += 1
    await db.commit()
    assert (
        await c.get("/auth/me", headers={"Authorization": f"Bearer {login['access_token']}"})
    ).status_code == 401


async def test_hub_introspection_checks_actor_access_live(pending_client, monkeypatch):
    from brain_api.config import get_settings

    monkeypatch.setattr(get_settings(), "SECRETARIA_API_KEY", "test-team-key")
    c, user, db = pending_client
    await activate(c)
    db.add(
        Entitlement(
            tenant_id=user.tenant_id,
            secretaria_enabled=True,
            precheck_enabled=False,
            status="active",
            plan="secretaria_basico",
        )
    )
    await db.commit()
    token = create_hub_token(
        tenant_id=str(user.tenant_id),
        actor_user_id=str(user.id),
        credential_version=user.credential_version,
    )
    headers = {"X-Internal-Api-Key": "test-team-key"}
    first = await c.post(
        "/internal/secretaria/hub-token/verify", json={"token": token}, headers=headers
    )
    assert first.json()["active"] is True
    user.access_revoked_at = datetime.now(UTC)
    await db.commit()
    second = await c.post(
        "/internal/secretaria/hub-token/verify", json={"token": token}, headers=headers
    )
    assert second.json()["active"] is False
