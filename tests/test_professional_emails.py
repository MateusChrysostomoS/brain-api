"""Tests for `GET /internal/tenants/{tenant_id}/professional-emails`.

The read that lets secretarIA email a professional about a new booking without
holding a copy of their address. brain-api is the single writer of identity, so
this is the ONLY place that answer exists — see `api/internal.py`.

Ground truth: api/internal.py, schemas/internal.py, models/user.py. Mirrors the
pair-key monkeypatch pattern from test_usage_events.py / test_auth_hardening.py.

The `client` fixture's seed is exactly the shape this endpoint has to
distinguish: tenant A's owner carries `professional_id`, tenant B's owner does
not. So "linked" and "not linked" are both real rows, not contrivances.
"""

from types import SimpleNamespace
from uuid import uuid4

import brain_api.api.internal as internal_api
from tests.test_rbac import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    CLINIC_A,
    CLINIC_B,
    OWNER_A_EMAIL,
    OWNER_A_PROFESSIONAL_ID,
    OWNER_B_EMAIL,
    _bearer,
    _token,
)

_PATH = "/internal/tenants/{tenant_id}/professional-emails"


def _set_pair_key(monkeypatch, key: str = "pair-key") -> None:
    fake_settings = SimpleNamespace(SECRETARIA_API_KEY=key, SECRETARIA_API_KEY_PREVIOUS="")
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)


async def _tenant_ids(client) -> dict[str, str]:
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()
    return {t["clinic_name"]: t["id"] for t in tenants["items"]}


# --- Pair-key gate ---------------------------------------------------------------


async def test_professional_emails_key_unset_forbidden(client):
    """SECRETARIA_API_KEY is forced empty by conftest -> fail closed with 403.

    An address list must never become readable by an unauthenticated caller
    just because the server forgot to configure its own key.
    """
    resp = await client.get(
        _PATH.format(tenant_id=uuid4()), headers={"X-Internal-Api-Key": "whatever"}
    )
    assert resp.status_code == 403


async def test_professional_emails_wrong_key_unauthorized(client, monkeypatch):
    _set_pair_key(monkeypatch)
    resp = await client.get(
        _PATH.format(tenant_id=uuid4()), headers={"X-Internal-Api-Key": "not-the-key"}
    )
    assert resp.status_code == 401


async def test_professional_emails_no_key_header_unauthorized(client, monkeypatch):
    _set_pair_key(monkeypatch)
    resp = await client.get(_PATH.format(tenant_id=uuid4()))
    assert resp.status_code == 401


# --- The answer ------------------------------------------------------------------


async def test_linked_professional_is_returned(client, monkeypatch):
    _set_pair_key(monkeypatch)
    tenant_id = (await _tenant_ids(client))[CLINIC_A]

    resp = await client.get(
        _PATH.format(tenant_id=tenant_id), headers={"X-Internal-Api-Key": "pair-key"}
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "items": [{"professional_id": str(OWNER_A_PROFESSIONAL_ID), "email": OWNER_A_EMAIL}]
    }


async def test_user_without_professional_id_is_absent(client, monkeypatch):
    """Tenant B's owner has no `professional_id`.

    Absent, not present-with-null: the endpoint answers "who can we reach as a
    professional", so a user who is not one at all — an owner before linkage,
    or a `secretary`, who by design never gets a professional_id — leaves no
    row here for a caller to mistakenly key on.
    """
    _set_pair_key(monkeypatch)
    tenant_id = (await _tenant_ids(client))[CLINIC_B]

    resp = await client.get(
        _PATH.format(tenant_id=tenant_id), headers={"X-Internal-Api-Key": "pair-key"}
    )

    assert resp.status_code == 200
    assert resp.json() == {"items": []}


async def test_unknown_tenant_is_empty_not_an_error(client, monkeypatch):
    """A tenant with nobody linked and a tenant that does not exist answer the
    same way. Distinguishing them would tell a caller which tenant ids are real
    without teaching it anything it needs."""
    _set_pair_key(monkeypatch)

    resp = await client.get(
        _PATH.format(tenant_id=uuid4()), headers={"X-Internal-Api-Key": "pair-key"}
    )

    assert resp.status_code == 200
    assert resp.json() == {"items": []}


async def test_tenant_isolation(client, monkeypatch):
    """Asking about tenant B never returns tenant A's address.

    The isolation invariant of the whole feature: this response decides who
    receives a "nova consulta marcada" email, so a leak here would mail one
    clinic's booking to another clinic's doctor.
    """
    _set_pair_key(monkeypatch)
    ids = await _tenant_ids(client)

    body = (
        await client.get(
            _PATH.format(tenant_id=ids[CLINIC_B]), headers={"X-Internal-Api-Key": "pair-key"}
        )
    ).json()

    emails = [item["email"] for item in body["items"]]
    assert OWNER_A_EMAIL not in emails
    assert OWNER_B_EMAIL not in emails  # not linked either — see the test above
