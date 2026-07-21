"""Auth-hardening round tests: refresh rotation/reuse, rate limiting, password policy,
scoped-token rejection, the secretarIA hub-token entitlement gate, internal token
introspection, and SECRET_KEY_PREVIOUS rotation.

Ground truth: core/security.py, services/auth.py, api/auth.py, api/deps.py,
api/internal.py, api/doctor.py, schemas/admin.py.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from jose import jwt as jose_jwt

import brain_api.api.auth as auth_api
import brain_api.api.internal as internal_api
import brain_api.core.security as security
from brain_api.api.deps import get_current_principal
from brain_api.config import get_settings
from brain_api.core.ratelimit import SlidingWindowLimiter
from brain_api.core.security import (
    create_access_token,
    create_hub_token,
    decode_hub_token,
    decode_token,
)
from tests.test_rbac import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    CLINIC_A,
    CLINIC_B,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    OWNER_A_PROFESSIONAL_ID,
    OWNER_B_EMAIL,
    OWNER_B_PASSWORD,
    _bearer,
    _token,
)


async def _tenant_ids(client, admin_token: str) -> dict[str, str]:
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()
    return {t["clinic_name"]: t["id"] for t in tenants["items"]}


# --- Login / refresh happy paths ---------------------------------------------


async def test_login_returns_refresh_and_expiry(client):
    resp = await client.post(
        "/auth/token", json={"email": OWNER_A_EMAIL, "password": OWNER_A_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["refresh_token"]
    assert body["expires_in"] == get_settings().ACCESS_TOKEN_EXPIRE_MINUTES * 60


async def test_refresh_happy_path(client):
    login = await client.post(
        "/auth/token", json={"email": OWNER_A_EMAIL, "password": OWNER_A_PASSWORD}
    )
    old_refresh = login.json()["refresh_token"]

    resp = await client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    new_refresh = body["refresh_token"]
    assert new_refresh != old_refresh

    me = await client.get("/auth/me", headers=_bearer(body["access_token"]))
    assert me.status_code == 200, me.text
    assert me.json()["user"]["email"] == OWNER_A_EMAIL


# --- Rotation + reuse detection -----------------------------------------------


async def test_refresh_rotation_reuse_revokes_family(client):
    login = await client.post(
        "/auth/token", json={"email": OWNER_A_EMAIL, "password": OWNER_A_PASSWORD}
    )
    old_refresh = login.json()["refresh_token"]

    first = await client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert first.status_code == 200, first.text
    new_refresh = first.json()["refresh_token"]

    # The OLD (already-rotated) token is now revoked.
    replay = await client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert replay.status_code == 401

    # Reuse of a revoked token is the theft signal: the WHOLE family is revoked, so the
    # legitimate successor token also stops working.
    second = await client.post("/auth/refresh", json={"refresh_token": new_refresh})
    assert second.status_code == 401


async def test_garbage_refresh_rejected(client):
    resp = await client.post("/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert resp.status_code == 401


# --- Logout --------------------------------------------------------------------


async def test_logout_revokes_and_is_idempotent(client):
    login = await client.post(
        "/auth/token", json={"email": OWNER_A_EMAIL, "password": OWNER_A_PASSWORD}
    )
    refresh_token = login.json()["refresh_token"]

    logout_resp = await client.post("/auth/logout", json={"refresh_token": refresh_token})
    assert logout_resp.status_code == 204

    after_logout = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert after_logout.status_code == 401

    # Logging out an unknown token is not an oracle: still 204.
    unknown_logout = await client.post("/auth/logout", json={"refresh_token": "never-issued-xyz"})
    assert unknown_logout.status_code == 204


# --- Rate limiting ---------------------------------------------------------------


async def test_login_rate_limited(client, monkeypatch):
    limiter = SlidingWindowLimiter("t", lambda: 2)
    monkeypatch.setattr(auth_api, "_limiter", limiter)

    bad_creds = {"email": OWNER_A_EMAIL, "password": "definitely-wrong"}
    first = await client.post("/auth/token", json=bad_creds)
    assert first.status_code == 401
    second = await client.post("/auth/token", json=bad_creds)
    assert second.status_code == 401
    third = await client.post("/auth/token", json=bad_creds)
    assert third.status_code == 429


# --- Password policy (POST /admin/users) ----------------------------------------


async def test_password_policy_admin_create_user(client):
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    base = {"name": "Pw Test", "role": "tenant_staff", "tenant_id": tenant_a_id}

    for bad_password, email in (
        ("12345678", "pw-digits-only@a.com"),
        ("abcdefghij", "pw-letters-only@a.com"),
        ("short1a", "pw-too-short@a.com"),
    ):
        resp = await client.post(
            "/admin/users",
            headers=_bearer(admin_token),
            json={**base, "email": email, "password": bad_password},
        )
        assert resp.status_code == 422, f"{bad_password!r} should be rejected: {resp.text}"

    ok_resp = await client.post(
        "/admin/users",
        headers=_bearer(admin_token),
        json={**base, "email": "pw-ok@a.com", "password": "abc12345"},
    )
    assert ok_resp.status_code == 201, ok_resp.text


# --- Scoped-token rejection --------------------------------------------------


async def test_scoped_hub_token_rejected_by_user_routes(client):
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    hub_token = create_hub_token(tenant_id=tenant_a_id, actor_user_id="x")

    resp_me = await client.get("/auth/me", headers=_bearer(hub_token))
    assert resp_me.status_code == 401

    resp_doctor = await client.get("/doctor/me", headers=_bearer(hub_token))
    assert resp_doctor.status_code != 200


# --- Hub-token mint: entitlement gate ------------------------------------------


async def test_hub_token_mint_gated_by_entitlement(client):
    owner_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    denied = await client.post("/doctor/secretaria/hub-token", headers=_bearer(owner_token))
    assert denied.status_code == 403
    assert denied.json()["detail"] == "secretaria_not_entitled"

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    patch = await client.patch(
        f"/admin/tenants/{tenant_a_id}/entitlements",
        headers=_bearer(admin_token),
        json={"plan": "complete_clinic_combo", "status": "active"},
    )
    assert patch.status_code == 200, patch.text

    granted = await client.post("/doctor/secretaria/hub-token", headers=_bearer(owner_token))
    assert granted.status_code == 200, granted.text
    body = granted.json()
    assert body["hub_token"]
    assert body["expires_in"] == get_settings().HUB_TOKEN_EXPIRE_MINUTES * 60


# --- Internal introspection: key gate -------------------------------------------


async def test_internal_hub_token_verify_key_unset_forbidden(client):
    """SECRETARIA_API_KEY is forced empty by conftest -> fail closed with 403."""
    resp = await client.post(
        "/internal/secretaria/hub-token/verify",
        headers={"X-Internal-Api-Key": "whatever"},
        json={"token": "irrelevant"},
    )
    assert resp.status_code == 403


async def test_internal_hub_token_verify_key_and_states(client, monkeypatch):
    fake_settings = SimpleNamespace(SECRETARIA_API_KEY="pair-key", SECRETARIA_API_KEY_PREVIOUS="")
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)

    wrong_key = await client.post(
        "/internal/secretaria/hub-token/verify",
        headers={"X-Internal-Api-Key": "not-the-key"},
        json={"token": "irrelevant"},
    )
    assert wrong_key.status_code == 401

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    patch = await client.patch(
        f"/admin/tenants/{tenant_a_id}/entitlements",
        headers=_bearer(admin_token),
        json={"plan": "complete_clinic_combo", "status": "active"},
    )
    assert patch.status_code == 200, patch.text
    hub_token = create_hub_token(tenant_id=tenant_a_id, actor_user_id="owner-a")

    entitled = await client.post(
        "/internal/secretaria/hub-token/verify",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"token": hub_token},
    )
    assert entitled.status_code == 200, entitled.text
    entitled_body = entitled.json()
    assert entitled_body["active"] is True
    assert entitled_body["tenant_id"] == tenant_a_id

    owner_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    as_user_jwt = await client.post(
        "/internal/secretaria/hub-token/verify",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"token": owner_token},
    )
    assert as_user_jwt.status_code == 200
    assert as_user_jwt.json()["active"] is False

    garbage = await client.post(
        "/internal/secretaria/hub-token/verify",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"token": "not-a-jwt-at-all"},
    )
    assert garbage.status_code == 200
    assert garbage.json()["active"] is False


async def test_internal_key_previous_rotation_window(client, monkeypatch):
    fake_settings = SimpleNamespace(SECRETARIA_API_KEY="new", SECRETARIA_API_KEY_PREVIOUS="old")
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)

    for key in ("new", "old"):
        resp = await client.post(
            "/internal/secretaria/hub-token/verify",
            headers={"X-Internal-Api-Key": key},
            json={"token": "irrelevant"},
        )
        assert resp.status_code == 200, f"{key}: {resp.text}"
        # The key was accepted (200, not 401); the garbage token itself just refuses.
        assert resp.json()["active"] is False


# --- Internal entitlement summary ---------------------------------------------


async def test_internal_tenant_entitlements_summary(client, monkeypatch):
    fake_settings = SimpleNamespace(SECRETARIA_API_KEY="pair-key", SECRETARIA_API_KEY_PREVIOUS="")
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    ids = await _tenant_ids(client, admin_token)
    tenant_a_id = ids[CLINIC_A]
    tenant_b_id = ids[CLINIC_B]

    patch = await client.patch(
        f"/admin/tenants/{tenant_a_id}/entitlements",
        headers=_bearer(admin_token),
        json={"plan": "secretaria_bronze_1", "status": "active", "secretaria_enabled": True},
    )
    assert patch.status_code == 200, patch.text

    resp = await client.get(
        f"/internal/tenants/{tenant_a_id}/entitlements",
        headers={"X-Internal-Api-Key": "pair-key"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"] is True
    assert body["plan"] == "secretaria_bronze_1"
    assert body["secretaria_tier"] == "bronze_1"
    assert isinstance(body["addons"], dict)

    resp_b = await client.get(
        f"/internal/tenants/{tenant_b_id}/entitlements",
        headers={"X-Internal-Api-Key": "pair-key"},
    )
    assert resp_b.status_code == 200, resp_b.text
    body_b = resp_b.json()
    assert body_b["plan"] == "free"
    assert body_b["active"] is False


# --- SECRET_KEY_PREVIOUS rotation (unit-level) ---------------------------------


async def test_secret_key_previous_rotation(monkeypatch):
    current_secret = "test-secret-key-not-for-production"
    old_secret = "old-secret"
    claims = {
        "sub": str(uuid4()),
        "tenant_id": str(uuid4()),
        "role": "tenant_owner",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    token_current = jose_jwt.encode(claims, current_secret, algorithm="HS256")
    token_old = jose_jwt.encode(claims, old_secret, algorithm="HS256")

    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(SECRET_KEY=current_secret, SECRET_KEY_PREVIOUS=old_secret),
    )
    assert decode_token(token_current) is not None
    assert decode_token(token_old) is not None

    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(SECRET_KEY=current_secret, SECRET_KEY_PREVIOUS=""),
    )
    assert decode_token(token_old) is None
    assert decode_token(token_current) is not None


# --- professional_id: token round-trips (STATE-MACHINE + AUTH build) -----------------
#
# Ground truth: core/security.py (create_access_token/create_hub_token), api/deps.py
# (Principal), schemas/auth.py + api/auth.py (login/refresh/exchange), api/internal.py
# (hub-token verify), api/doctor.py (hub-token mint). CONTRACT_onboarding_v1.md §6.


def test_access_token_round_trips_professional_id():
    pid = str(uuid4())
    token = create_access_token(
        sub=str(uuid4()), tenant_id=str(uuid4()), role="tenant_owner", professional_id=pid
    )
    claims = decode_token(token)
    assert claims is not None
    assert claims["professional_id"] == pid


def test_access_token_omits_professional_id_when_none():
    token = create_access_token(sub=str(uuid4()), tenant_id=str(uuid4()), role="tenant_owner")
    claims = decode_token(token)
    assert claims is not None
    assert "professional_id" not in claims

    # Explicit None behaves the same as simply omitting the keyword.
    token_explicit_none = create_access_token(
        sub=str(uuid4()), tenant_id=str(uuid4()), role="tenant_owner", professional_id=None
    )
    assert "professional_id" not in decode_token(token_explicit_none)


def test_hub_token_round_trips_professional_id():
    pid = str(uuid4())
    token = create_hub_token(tenant_id=str(uuid4()), actor_user_id="u1", professional_id=pid)
    claims = decode_hub_token(token)
    assert claims is not None
    assert claims["professional_id"] == pid


def test_hub_token_omits_professional_id_when_none():
    token = create_hub_token(tenant_id=str(uuid4()), actor_user_id="u1")
    claims = decode_hub_token(token)
    assert claims is not None
    assert "professional_id" not in claims


def test_principal_parses_valid_professional_id_claim():
    pid = uuid4()
    token = create_access_token(
        sub=str(uuid4()), tenant_id=str(uuid4()), role="tenant_owner", professional_id=str(pid)
    )
    principal = get_current_principal(f"Bearer {token}")
    assert principal.professional_id == pid


def test_principal_professional_id_none_when_absent():
    token = create_access_token(sub=str(uuid4()), tenant_id=str(uuid4()), role="tenant_owner")
    principal = get_current_principal(f"Bearer {token}")
    assert principal.professional_id is None


def test_principal_treats_invalid_professional_id_claim_as_none():
    """A malformed claim must never 401 the whole token — it just reads as absent."""
    settings = get_settings()
    claims = {
        "sub": str(uuid4()),
        "tenant_id": str(uuid4()),
        "role": "tenant_owner",
        "professional_id": "not-a-uuid",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    token = jose_jwt.encode(claims, settings.SECRET_KEY, algorithm="HS256")
    principal = get_current_principal(f"Bearer {token}")
    assert principal.professional_id is None
    assert principal.role == "tenant_owner"  # the rest of the token is still valid


# --- professional_id: /auth/me + login/refresh response exposure ---------------------


async def test_auth_me_exposes_professional_id_when_present(client):
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/auth/me", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["professional_id"] == str(OWNER_A_PROFESSIONAL_ID)


async def test_auth_me_professional_id_null_when_absent(client):
    token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    resp = await client.get("/auth/me", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["professional_id"] is None


async def test_login_response_exposes_professional_id_and_name(client):
    resp = await client.post(
        "/auth/token", json={"email": OWNER_A_EMAIL, "password": OWNER_A_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["professional_id"] == str(OWNER_A_PROFESSIONAL_ID)
    assert body["name"] == "Owner A"

    # The claim is ALSO embedded directly on the access token itself.
    claims = decode_token(body["access_token"])
    assert claims["professional_id"] == str(OWNER_A_PROFESSIONAL_ID)


async def test_login_response_professional_id_null_when_absent(client):
    resp = await client.post(
        "/auth/token", json={"email": OWNER_B_EMAIL, "password": OWNER_B_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["professional_id"] is None
    assert "professional_id" not in decode_token(body["access_token"])


async def test_refresh_response_exposes_professional_id(client):
    login = await client.post(
        "/auth/token", json={"email": OWNER_A_EMAIL, "password": OWNER_A_PASSWORD}
    )
    refresh_token = login.json()["refresh_token"]
    resp = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200, resp.text
    assert resp.json()["professional_id"] == str(OWNER_A_PROFESSIONAL_ID)


# --- professional_id: hub-token mint + internal introspection passthrough ------------


async def test_hub_token_mint_carries_professional_id(client):
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    patch = await client.patch(
        f"/admin/tenants/{tenant_a_id}/entitlements",
        headers=_bearer(admin_token),
        json={"plan": "complete_clinic_combo", "status": "active"},
    )
    assert patch.status_code == 200, patch.text

    owner_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    granted = await client.post("/doctor/secretaria/hub-token", headers=_bearer(owner_token))
    assert granted.status_code == 200, granted.text

    claims = decode_hub_token(granted.json()["hub_token"])
    assert claims is not None
    assert claims["professional_id"] == str(OWNER_A_PROFESSIONAL_ID)


async def test_internal_hub_token_verify_passes_professional_id(client, monkeypatch):
    fake_settings = SimpleNamespace(SECRETARIA_API_KEY="pair-key", SECRETARIA_API_KEY_PREVIOUS="")
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    patch = await client.patch(
        f"/admin/tenants/{tenant_a_id}/entitlements",
        headers=_bearer(admin_token),
        json={"plan": "complete_clinic_combo", "status": "active"},
    )
    assert patch.status_code == 200, patch.text
    hub_token = create_hub_token(
        tenant_id=tenant_a_id,
        actor_user_id="owner-a",
        professional_id=str(OWNER_A_PROFESSIONAL_ID),
    )

    resp = await client.post(
        "/internal/secretaria/hub-token/verify",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"token": hub_token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"] is True
    assert body["professional_id"] == str(OWNER_A_PROFESSIONAL_ID)


async def test_internal_hub_token_verify_professional_id_null_when_absent(client, monkeypatch):
    fake_settings = SimpleNamespace(SECRETARIA_API_KEY="pair-key", SECRETARIA_API_KEY_PREVIOUS="")
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)
    hub_token = create_hub_token(tenant_id=str(uuid4()), actor_user_id="x")

    resp = await client.post(
        "/internal/secretaria/hub-token/verify",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"token": hub_token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["professional_id"] is None


async def test_internal_hub_token_verify_invalid_professional_id_claim_is_safe(client, monkeypatch):
    """A malformed professional_id claim on the hub token must not 500 the endpoint."""
    fake_settings = SimpleNamespace(SECRETARIA_API_KEY="pair-key", SECRETARIA_API_KEY_PREVIOUS="")
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)

    settings = get_settings()
    claims = {
        "sub": str(uuid4()),
        "scope": security.HUB_TOKEN_SCOPE,
        "act": "x",
        "professional_id": "not-a-uuid",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    token = jose_jwt.encode(claims, settings.SECRET_KEY, algorithm="HS256")

    resp = await client.post(
        "/internal/secretaria/hub-token/verify",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["professional_id"] is None
