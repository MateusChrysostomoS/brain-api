"""TASK-020: `POST /internal/secretaria/hub-token/verify` hands the role to secretarIA.

The role travels back in the introspection body (secretarIA never decodes the token).
brain-api does not INTERPRET it here — secretarIA is the policy point — but it must only
ever pass through well-formed values and never 500 on a malformed claim.
"""

from types import SimpleNamespace

import pytest

import brain_api.api.internal as internal_api
from tests.hub_tokens import (
    forge_hub_token,
    make_hub_token,
    tamper_hub_token,
    unsigned_hub_token,
)
from tests.test_auth_hardening import _tenant_ids
from tests.test_rbac import ADMIN_EMAIL, ADMIN_PASSWORD, CLINIC_A, _bearer, _token

_PAIR = {"X-Internal-Api-Key": "pair-key"}


@pytest.fixture(autouse=True)
def _pair_key(monkeypatch):
    fake = SimpleNamespace(SECRETARIA_API_KEY="pair-key", SECRETARIA_API_KEY_PREVIOUS="")
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake)


async def _entitled_tenant(client) -> str:
    """CLINIC_A with secretarIA on, so `active` is true for a valid hub token."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    patch = await client.patch(
        f"/admin/tenants/{tenant_id}/entitlements",
        headers=_bearer(admin_token),
        json={"plan": "complete_clinic_combo", "status": "active"},
    )
    assert patch.status_code == 200, patch.text
    return tenant_id


async def _verify(client, token: str):
    resp = await client.post(
        "/internal/secretaria/hub-token/verify", headers=_PAIR, json={"token": token}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize(
    ("role", "is_owner", "is_manager"),
    [
        ("manager", False, True),
        ("doctor", True, True),
        ("doctor", False, False),
        ("secretary", False, False),
    ],
)
async def test_verify_returns_the_role_claims(client, role, is_owner, is_manager):
    tenant_id = await _entitled_tenant(client)
    token = make_hub_token(role=role, is_owner=is_owner, is_manager=is_manager, tenant_id=tenant_id)

    body = await _verify(client, token)

    assert body["active"] is True
    assert body["tenant_id"] == tenant_id
    assert body["role"] == role
    assert body["is_owner"] is is_owner
    assert body["is_manager"] is is_manager


async def test_old_token_without_role_verifies_with_null_role(client):
    """The deploy window: a token minted before TASK-020 stays valid, with no role."""
    tenant_id = await _entitled_tenant(client)

    body = await _verify(client, make_hub_token(role=None, tenant_id=tenant_id))

    assert body["active"] is True
    assert body["role"] is None
    assert body["is_owner"] is None
    assert body["is_manager"] is None


async def test_unknown_role_value_is_passed_through_for_secretaria_to_refuse(client):
    tenant_id = await _entitled_tenant(client)
    token = forge_hub_token({"sub": tenant_id, "role": "admin", "is_manager": True})

    body = await _verify(client, token)

    assert body["active"] is True
    assert body["role"] == "admin"


@pytest.mark.parametrize(
    "claims",
    [
        {"role": 123},
        {"role": ["manager"]},
        {"role": True},
        {"role": ""},
        {"role": "m" * 33},
    ],
)
async def test_malformed_role_reads_as_absent_and_never_500s(client, claims):
    tenant_id = await _entitled_tenant(client)

    body = await _verify(client, forge_hub_token({"sub": tenant_id, **claims}))

    assert body["active"] is True
    assert body["role"] is None


@pytest.mark.parametrize("value", ["true", 1, "yes", [True]])
async def test_non_boolean_flags_read_as_null(client, value):
    tenant_id = await _entitled_tenant(client)
    token = forge_hub_token(
        {"sub": tenant_id, "role": "doctor", "is_owner": value, "is_manager": value}
    )

    body = await _verify(client, token)

    assert body["role"] == "doctor"
    assert body["is_owner"] is None
    assert body["is_manager"] is None


async def test_payload_swapped_with_old_signature_is_inactive_with_no_role(client):
    tenant_id = await _entitled_tenant(client)
    token = make_hub_token(role="secretary", tenant_id=tenant_id)

    body = await _verify(client, tamper_hub_token(token, role="manager", is_manager=True))

    assert body["active"] is False
    assert body["role"] is None


async def test_alg_none_token_is_inactive(client):
    tenant_id = await _entitled_tenant(client)

    body = await _verify(client, unsigned_hub_token(sub=tenant_id, role="manager"))

    assert body["active"] is False
    assert body["role"] is None


async def test_token_signed_with_another_key_is_inactive(client):
    tenant_id = await _entitled_tenant(client)
    forged = forge_hub_token(
        {"sub": tenant_id, "role": "manager", "is_manager": True},
        secret="not-the-real-secret-key-not-the-real",
    )

    body = await _verify(client, forged)

    assert body["active"] is False


async def test_user_jwt_is_not_a_hub_token(client):
    """A user access token (no scope) still verifies inactive; its role is never echoed."""
    user_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    body = await _verify(client, user_token)

    assert body["active"] is False
    assert body["role"] is None
