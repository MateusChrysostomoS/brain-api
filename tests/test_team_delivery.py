"""Late mail delivery cannot overwrite a newer first-access generation."""

from datetime import UTC, datetime

from brain_api.config import get_settings
from tests.test_staff_first_access import pending_client  # noqa: F401


async def test_delivery_status_uses_generation_and_active_access(pending_client, monkeypatch):
    c, user, db = pending_client
    monkeypatch.setattr(get_settings(), "SECRETARIA_API_KEY", "test-delivery-key")
    headers = {"X-Internal-Api-Key": "test-delivery-key"}
    path = f"/internal/tenants/{user.tenant_id}/team/{user.id}/delivery"
    response = await c.get(path + "?credential_version=0", headers=headers)
    assert response.status_code == 200
    assert response.json()["deliverable"] is True
    assert (
        await c.post(path, headers=headers, json={"credential_version": 0, "status": "sent"})
    ).status_code == 204
    await db.refresh(user)
    assert user.invite_delivery_status == "sent"
    user.credential_version = 1
    user.invite_delivery_status = "queued"
    await db.commit()
    await c.post(path, headers=headers, json={"credential_version": 0, "status": "failed"})
    await db.refresh(user)
    assert user.invite_delivery_status == "queued"
    assert (await c.get(path + "?credential_version=0", headers=headers)).json()[
        "deliverable"
    ] is False
    user.access_revoked_at = datetime.now(UTC)
    await db.commit()
    assert (await c.get(path + "?credential_version=1", headers=headers)).json()[
        "deliverable"
    ] is False
