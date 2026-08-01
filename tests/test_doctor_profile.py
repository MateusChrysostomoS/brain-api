"""Doctor self-edit profile tests (`PATCH /doctor/me`, "Meu Perfil" foundation round).

Covers: editing the caller's own name (persists, reflected on a fresh GET), rejecting an
attempt to smuggle in email/role (422 — the schema's `extra="forbid"`, never a manual
field filter), blank-after-trim name (422), no-auth (401), admin token (403 — same
router-level `require_doctor` gate as every other `/doctor/*` route), and tenant
isolation (editing as Owner A never touches Owner B's row).
"""

from tests.test_rbac import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    CLINIC_A,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    OWNER_B_EMAIL,
    OWNER_B_PASSWORD,
    _bearer,
    _token,
)


async def test_update_name_ok(client) -> None:
    """Happy path: name changes, everything else on the response is unchanged."""
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.patch("/doctor/me", headers=_bearer(token), json={"name": "Nova Doutora"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["name"] == "Nova Doutora"
    assert body["user"]["email"] == OWNER_A_EMAIL
    assert body["user"]["role"] == "tenant_owner"
    assert body["tenant"]["clinic_name"] == CLINIC_A

    # Persisted — a fresh GET (a separate request) reflects it too.
    me = await client.get("/doctor/me", headers=_bearer(token))
    assert me.json()["user"]["name"] == "Nova Doutora"


async def test_update_name_trims_whitespace(client) -> None:
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.patch("/doctor/me", headers=_bearer(token), json={"name": "  Com Espaço  "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["name"] == "Com Espaço"


async def test_update_name_blank_rejected(client) -> None:
    """Whitespace-only input min_length(1) would let through is still rejected (422)."""
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.patch("/doctor/me", headers=_bearer(token), json={"name": "   "})
    assert resp.status_code == 422

    empty = await client.patch("/doctor/me", headers=_bearer(token), json={"name": ""})
    assert empty.status_code == 422

    missing = await client.patch("/doctor/me", headers=_bearer(token), json={})
    assert missing.status_code == 422


async def test_update_rejects_email_and_role(client) -> None:
    """The schema's extra="forbid" rejects email/role/tenant_id — not a manual filter."""
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)

    resp = await client.patch(
        "/doctor/me",
        headers=_bearer(token),
        json={"name": "Nome Ok", "email": "outro@x.com"},
    )
    assert resp.status_code == 422

    resp2 = await client.patch(
        "/doctor/me",
        headers=_bearer(token),
        json={"name": "Nome Ok", "role": "admin"},
    )
    assert resp2.status_code == 422

    resp3 = await client.patch(
        "/doctor/me",
        headers=_bearer(token),
        json={"name": "Nome Ok", "tenant_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert resp3.status_code == 422

    resp4 = await client.patch(
        "/doctor/me",
        headers=_bearer(token),
        json={"name": "Nome Ok", "password": "whatever123"},
    )
    assert resp4.status_code == 422

    # None of the rejected attempts changed anything server-side.
    me = await client.get("/doctor/me", headers=_bearer(token))
    assert me.json()["user"]["email"] == OWNER_A_EMAIL
    assert me.json()["user"]["role"] == "tenant_owner"
    assert me.json()["user"]["name"] == "Owner A"


async def test_update_requires_auth(client) -> None:
    resp = await client.patch("/doctor/me", json={"name": "Sem Token"})
    assert resp.status_code == 401


async def test_update_rejects_admin_token(client) -> None:
    """Same router-level require_doctor gate as every other /doctor/* route: 403 for admin."""
    token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    resp = await client.patch("/doctor/me", headers=_bearer(token), json={"name": "X"})
    assert resp.status_code == 403


async def test_update_does_not_leak_across_tenants(client) -> None:
    """Editing as Owner A never touches Owner B's row (tenant scoping via the token)."""
    token_a = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.patch("/doctor/me", headers=_bearer(token_a), json={"name": "Só A Mudou"})
    assert resp.status_code == 200, resp.text

    token_b = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    me_b = await client.get("/doctor/me", headers=_bearer(token_b))
    assert me_b.json()["user"]["name"] == "Owner B"
