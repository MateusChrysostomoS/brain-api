"""Admin "Modo médico" impersonation (POST /admin/impersonate/token).

Reuses the seeded in-memory app + tokens from test_rbac's `client` fixture (provided via
conftest re-export, injected by name). Asserts the RBAC gate, the "target not seeded" 404,
and — the important one — that the minted token is a REAL tenant-scoped doctor token: it
works on /doctor/me + /entitlements for the target clinic and is NOT an admin token.
"""

import pytest

from brain_api.config import get_settings
from brain_api.core.security import decode_token

# Helpers/constants from the RBAC suite; `client` is injected by name (conftest re-export).
from tests.test_rbac import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    CLINIC_A,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    _bearer,
    _token,
)

IMPERSONATE = "/admin/impersonate/token"


async def test_impersonate_requires_admin(client):
    """The mint is admin-only: 403 for a tenant token, 401 with no token."""
    owner_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    assert (await client.post(IMPERSONATE, headers=_bearer(owner_token))).status_code == 403
    assert (await client.post(IMPERSONATE)).status_code == 401


async def test_impersonate_target_unavailable_404(client):
    """With the default demo email (not seeded in the test DB), an admin gets a clean 404."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    resp = await client.post(IMPERSONATE, headers=_bearer(admin_token))
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "impersonation_target_unavailable"


async def test_impersonate_mints_working_doctor_token(
    client, monkeypatch: pytest.MonkeyPatch
):
    """A configured target yields a real tenant-scoped doctor token (not the admin's).

    Point IMPERSONATION_DEMO_EMAIL at the seeded owner of Clínica A, then verify the minted
    token: (1) carries that user's identity/tenant/role, (2) actually works on the doctor
    surface for Clínica A, and (3) is rejected by the admin surface — proving the admin truly
    "became" the doctor rather than reusing their admin token.
    """
    # The cached Settings is a singleton; overriding the attr affects every get_settings().
    monkeypatch.setattr(get_settings(), "IMPERSONATION_DEMO_EMAIL", OWNER_A_EMAIL)

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    resp = await client.post(IMPERSONATE, headers=_bearer(admin_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["token_type"] == "bearer"
    assert body["clinic_name"] == CLINIC_A
    assert body["email"] == OWNER_A_EMAIL
    assert body["role"] == "tenant_owner"
    assert body["expires_in"] == get_settings().ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert "password_hash" not in str(body)

    # The minted token carries the DOCTOR's identity + tenant + role (not the admin's).
    claims = decode_token(body["access_token"])
    assert claims is not None
    assert claims["tenant_id"] == body["tenant_id"]
    assert claims["role"] == "tenant_owner"

    minted = _bearer(body["access_token"])

    # (2) It works on the doctor surface, scoped to Clínica A.
    me = await client.get("/doctor/me", headers=minted)
    assert me.status_code == 200, me.text
    assert me.json()["tenant"]["clinic_name"] == CLINIC_A
    assert me.json()["user"]["email"] == OWNER_A_EMAIL

    ent = await client.get("/entitlements", headers=minted)
    assert ent.status_code == 200, ent.text
    assert ent.json()["clinic_name"] == CLINIC_A

    # (3) It is NOT an admin token — the admin surface rejects it (wrong portal / role).
    assert (await client.get("/admin/tenants", headers=minted)).status_code == 403
