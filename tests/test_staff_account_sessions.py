"""New account contract and clinical gate isolation, using the actual ASGI API."""

import pytest

from brain_api.core.security import decode_token
from tests.test_rbac import OWNER_A_EMAIL, OWNER_A_PASSWORD

pytestmark = pytest.mark.usefixtures("staff_accounts_enabled")


async def test_account_login_requires_selection_even_one_membership(client):
    result = await client.post(
        "/auth/account/login",
        json={
            "email": OWNER_A_EMAIL,
            "password": OWNER_A_PASSWORD,
        },
    )
    assert result.status_code == 200
    body = result.json()
    assert body["selection_state"] == "selection_required"
    assert body["clinical_session"] is None
    proof = {"Authorization": f"Bearer {body['account_token']}"}
    assert (await client.get("/auth/me", headers=proof)).status_code == 401
    membership = body["memberships"][0]
    selected = await client.post(
        "/auth/account/select",
        headers=proof,
        json={
            "user_id": membership["user_id"],
            "remember": True,
        },
    )
    assert selected.status_code == 200
    clinical = selected.json()
    assert clinical["refresh_token"] is None
    claims = decode_token(clinical["access_token"])
    assert claims["sub"] == membership["user_id"]
    assert claims["tenant_id"] == membership["tenant_id"]
    assert claims["account_sid"]
    assert (
        await client.get(
            "/auth/me", headers={"Authorization": f"Bearer {clinical['access_token']}"}
        )
    ).status_code == 200
    assert (
        await client.post(
            "/auth/account/select",
            headers=proof,
            json={
                "user_id": membership["user_id"],
                "tenant_id": membership["tenant_id"],
            },
        )
    ).status_code == 422


async def test_account_logout_invalidates_selected_token(client):
    body = (
        await client.post(
            "/auth/account/login",
            json={
                "email": OWNER_A_EMAIL,
                "password": OWNER_A_PASSWORD,
            },
        )
    ).json()
    selected = (
        await client.post(
            "/auth/account/select",
            json={
                "user_id": body["memberships"][0]["user_id"],
            },
            headers={"Authorization": f"Bearer {body['account_token']}"},
        )
    ).json()
    assert (
        await client.post(
            "/auth/account/logout", headers={"Authorization": f"Bearer {body['account_token']}"}
        )
    ).status_code == 204
    assert (
        await client.get(
            "/auth/me", headers={"Authorization": f"Bearer {selected['access_token']}"}
        )
    ).status_code == 401


async def test_legacy_logout_cannot_resume_a_personal_cookie(client):
    client.base_url = "https://test"
    result = await client.post(
        "/auth/account/login",
        json={
            "email": OWNER_A_EMAIL,
            "password": OWNER_A_PASSWORD,
        },
    )
    assert result.status_code == 200
    assert (await client.post("/auth/logout")).status_code == 204
    resumed = await client.post("/auth/account/resume", headers={"X-Brain-Client": "web"})
    assert resumed.status_code == 401


async def test_legacy_login_cannot_inherit_another_personal_cookie(client):
    from tests.test_rbac import ADMIN_EMAIL, ADMIN_PASSWORD

    client.base_url = "https://test"
    assert (
        await client.post(
            "/auth/account/login",
            json={
                "email": OWNER_A_EMAIL,
                "password": OWNER_A_PASSWORD,
            },
        )
    ).status_code == 200
    assert (
        await client.post(
            "/auth/token",
            json={
                "email": ADMIN_EMAIL,
                "password": ADMIN_PASSWORD,
            },
        )
    ).status_code == 200
    assert client.cookies.get("__Host-staff_account") is None


async def test_legacy_invite_writer_uses_restricted_first_access_when_enabled(
    client, monkeypatch, mail_spy
):
    from uuid import uuid4

    from brain_api.config import get_settings
    from brain_api.services import secretaria_provisioning

    monkeypatch.setattr(get_settings(), "BRAIN_MESSAGE_PUBLIC_URL", "https://portal.example.com")

    async def create(*args, **kwargs):
        return {"professional_id": str(uuid4()), "created": True}

    monkeypatch.setattr(secretaria_provisioning, "create_professional", create)
    owner = (
        await client.post(
            "/auth/token",
            json={
                "email": OWNER_A_EMAIL,
                "password": OWNER_A_PASSWORD,
            },
        )
    ).json()
    invited = await client.post(
        "/doctor/professionals/invites",
        headers={"Authorization": f"Bearer {owner['access_token']}"},
        json={"name": "Doc New", "email": "new057@example.com"},
    )
    assert invited.status_code == 201
    assert invited.json()["invite_link"].endswith("/login")
    temporary = mail_spy.of("staff_temporary_password")[-1][2]["temporary_password"]
    setup = (
        await client.post(
            "/auth/token",
            json={
                "email": "new057@example.com",
                "password": temporary,
            },
        )
    ).json()
    assert setup["requires_password_change"]
    assert setup["refresh_token"] is None
    assert (
        await client.get("/auth/me", headers={"Authorization": f"Bearer {setup['access_token']}"})
    ).status_code == 401
