"""TASK-020: `POST /doctor/secretaria/hub-token` mints the CALLER's role (brain-api half).

Reuses the `test_secretary_role` world: one tenant entitled to both products with an owner
doctor (`is_owner` + `is_manager`), a plain doctor, and a secretary. A `manager`-role user is
added here because that fixture has none.
"""

import pytest

from brain_api.core.security import decode_hub_token, hash_password
from brain_api.models import User
from brain_api.models.user import ROLE_MANAGER
from tests.hub_tokens import make_hub_token
from tests.test_secretary_role import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    OWNER_EMAIL,
    OWNER_PASSWORD,
    SECRETARY_EMAIL,
    SECRETARY_PASSWORD,
    STAFF_EMAIL,
    STAFF_PASSWORD,
    _bearer,
    _token,
    client as client,  # pytest fixture re-export
)

MANAGER_EMAIL, MANAGER_PASSWORD = "gestor@clinic.com", "gestorpass1"

# who -> (email, password, expected role, expected is_owner, expected is_manager)
CALLERS = {
    "owner-doctor": (OWNER_EMAIL, OWNER_PASSWORD, "doctor", True, True),
    "plain-doctor": (STAFF_EMAIL, STAFF_PASSWORD, "doctor", False, False),
    "manager": (MANAGER_EMAIL, MANAGER_PASSWORD, "manager", False, True),
    "secretary": (SECRETARY_EMAIL, SECRETARY_PASSWORD, "secretary", False, False),
}


async def _add_manager(client) -> None:
    async with client.sessionmaker() as session, session.begin():
        session.add(
            User(
                tenant_id=client.tenant_id,
                email=MANAGER_EMAIL,
                name="Gestor",
                password_hash=hash_password(MANAGER_PASSWORD),
                role=ROLE_MANAGER,
                is_manager=True,
            )
        )


async def _mint(client, email: str, password: str, **kwargs):
    token = await _token(client, email, password)
    return await client.post("/doctor/secretaria/hub-token", headers=_bearer(token), **kwargs)


@pytest.mark.parametrize("who", sorted(CALLERS))
async def test_minted_hub_token_carries_the_callers_role(client, who):
    await _add_manager(client)
    email, password, role, is_owner, is_manager = CALLERS[who]

    resp = await _mint(client, email, password)

    assert resp.status_code == 200, resp.text
    claims = decode_hub_token(resp.json()["hub_token"])
    assert claims is not None
    assert claims["sub"] == str(client.tenant_id)
    assert "tenant_id" not in claims
    assert claims["role"] == role
    assert claims["is_owner"] is is_owner
    assert claims["is_manager"] is is_manager


async def test_request_body_cannot_upgrade_the_minted_role(client):
    resp = await _mint(
        client,
        SECRETARY_EMAIL,
        SECRETARY_PASSWORD,
        json={"role": "manager", "is_owner": True, "is_manager": True},
    )

    assert resp.status_code == 200, resp.text
    claims = decode_hub_token(resp.json()["hub_token"])
    assert claims["role"] == "secretary"
    assert claims["is_owner"] is False
    assert claims["is_manager"] is False


async def test_platform_admin_gets_no_hub_token(client):
    resp = await _mint(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Doctor access required"


async def test_hub_token_with_a_role_is_still_not_a_user_session(client):
    """A role claim must not turn the hub token into something the user routes accept."""
    token = make_hub_token(role="manager", is_manager=True, tenant_id=str(client.tenant_id))

    assert (await client.get("/auth/me", headers=_bearer(token))).status_code == 401
    assert (await client.get("/doctor/me", headers=_bearer(token))).status_code == 401
