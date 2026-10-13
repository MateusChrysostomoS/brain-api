"""Clinic-scoped team administration and first-access delivery; no external calls."""

from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from brain_api.config import get_settings
from brain_api.core.security import verify_password
from brain_api.models import Tenant, User
from brain_api.services import secretaria_provisioning
from tests.test_staff_first_access import pending_client, temp_login  # noqa: F401


@pytest_asyncio.fixture
async def team_client(pending_client, monkeypatch):
    c, owner, db = pending_client
    owner.requires_password_change = False
    owner.temporary_password_expires_at = None
    owner.is_owner = True
    owner.is_manager = True
    await db.commit()
    monkeypatch.setattr(
        get_settings(), "BRAIN_MESSAGE_PUBLIC_URL", "https://portal.example.com/"
    )
    captured = []

    async def send(to, template, variables):
        captured.append({"to": to, "template": template, "variables": variables})
        return True

    async def create(tenant_id, *, name, specialty, about, **kwargs):
        return {"professional_id": str(uuid4()), "created": True}

    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", send)
    monkeypatch.setattr(secretaria_provisioning, "create_professional", create)
    token = (await temp_login(c)).json()["access_token"]
    yield c, owner, db, {"Authorization": f"Bearer {token}"}, captured


@pytest.mark.parametrize(
    "role,is_manager",
    [("doctor", False), ("doctor", True), ("manager", True), ("secretary", False)],
)
async def test_invite_roles_receive_private_temporary_password(team_client, role, is_manager):
    c, owner, db, headers, emails = team_client
    response = await c.post(
        "/doctor/team/invites",
        headers=headers,
        json={
            "name": "New Staff",
            "email": "new@example.com",
            "role": role,
            "is_manager": is_manager,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["member"]["access_status"] == "pending_first_access"
    assert body["member"]["role"] == role
    assert body["member"]["is_manager"] is is_manager
    assert bool(body["member"]["professional_id"]) is (role == "doctor")
    user = await db.get(User, UUID(body["member"]["user_id"]))
    password = emails[0]["variables"]["temporary_password"]
    assert verify_password(password, user.password_hash)
    assert password != "TemporaryTest7"
    assert password not in response.text
    assert "password_hash" not in response.text
    assert emails[0]["variables"]["login_url"] == "https://portal.example.com/login"
    logged = await c.post("/auth/token", json={"email": "new@example.com", "password": password})
    assert logged.status_code == 200 and logged.json()["requires_password_change"] is True


async def test_team_management_restricted_and_clinic_scoped(team_client):
    c, owner, db, headers, emails = team_client
    owner.is_owner = False
    owner.is_manager = False
    await db.commit()
    assert (await c.get("/doctor/team", headers=headers)).status_code == 403
    owner.is_owner = True
    await db.commit()
    tenant = Tenant(clinic_name="Other Clinic")
    db.add(tenant)
    await db.flush()
    foreign = User(
        tenant_id=tenant.id,
        name="Foreign",
        email="foreign@example.com",
        password_hash=owner.password_hash,
        role="doctor",
    )
    db.add(foreign)
    await db.commit()
    for action in ("revoke", "first-access/resend"):
        assert (
            await c.post(f"/doctor/team/{foreign.id}/{action}", headers=headers)
        ).status_code == 404
    assert (
        await c.patch(
            f"/doctor/team/{foreign.id}/management", headers=headers, json={"is_manager": True}
        )
    ).status_code == 404
    assert (await c.post(f"/doctor/team/{owner.id}/revoke", headers=headers)).status_code == 409


async def test_secretary_cannot_grant_access_even_with_inconsistent_owner_flag(team_client):
    c, owner, db, headers, emails = team_client
    owner.role = "secretary"
    await db.commit()
    assert (await c.get("/doctor/team", headers=headers)).status_code == 403


async def test_resend_changes_generation_and_old_password_is_invalid(team_client):
    c, owner, db, headers, emails = team_client
    body = (
        await c.post(
            "/doctor/team/invites",
            headers=headers,
            json={"name": "Desk", "email": "desk@example.com", "role": "secretary"},
        )
    ).json()
    old = emails[-1]["variables"]["temporary_password"]
    resent = await c.post(
        f"/doctor/team/{body['member']['user_id']}/first-access/resend", headers=headers
    )
    assert resent.status_code == 200
    new = emails[-1]["variables"]["temporary_password"]
    assert new != old
    assert (
        await c.post("/auth/token", json={"email": "desk@example.com", "password": old})
    ).status_code == 401
    assert (
        await c.post("/auth/token", json={"email": "desk@example.com", "password": new})
    ).status_code == 200


async def test_mail_failure_does_not_claim_sent_or_duplicate_user(team_client, monkeypatch):
    c, owner, db, headers, emails = team_client

    async def fail(*args, **kwargs):
        return False

    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", fail)
    data = {"name": "Desk", "email": "desk@example.com", "role": "secretary"}
    response = await c.post("/doctor/team/invites", headers=headers, json=data)
    assert response.status_code == 201
    assert response.json()["delivery_status"] == "failed"
    assert (await c.post("/doctor/team/invites", headers=headers, json=data)).status_code == 409


async def test_doctor_can_be_manager_without_losing_professional_link(team_client):
    c, owner, db, headers, emails = team_client
    body = (
        await c.post(
            "/doctor/team/invites",
            headers=headers,
            json={"name": "Doctor", "email": "doctor@example.com", "role": "doctor"},
        )
    ).json()
    member = body["member"]
    updated = await c.patch(
        f"/doctor/team/{member['user_id']}/management", headers=headers, json={"is_manager": True}
    )
    assert updated.status_code == 200
    assert updated.json()["is_manager"] is True
    assert updated.json()["professional_id"] == member["professional_id"]
    assert updated.json()["role"] == "doctor"
