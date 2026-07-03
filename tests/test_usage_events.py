"""Tests for `POST /internal/usage-events` — the metering leg (stripe-billing-entitlements
skill). No Stripe call anywhere on this path; verifies the pair-key gate, the idempotent
ledger insert + `entitlements.usage` increment, and the catalog-backed feature validation.

Ground truth: api/internal.py, services/usage.py, models/usage_event.py,
schemas/internal.py. Mirrors the pair-key monkeypatch pattern from test_auth_hardening.py.
"""

from types import SimpleNamespace
from uuid import uuid4

import brain_api.api.internal as internal_api
from brain_api.services.catalog import LIMIT_MESSAGES, LIMIT_REMINDERS
from tests.test_rbac import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    CLINIC_A,
    CLINIC_B,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    _bearer,
    _token,
)


async def _tenant_ids(client, admin_token: str) -> dict[str, str]:
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()
    return {t["clinic_name"]: t["id"] for t in tenants["items"]}


def _set_pair_key(monkeypatch, key: str = "pair-key") -> None:
    fake_settings = SimpleNamespace(SECRETARIA_API_KEY=key, SECRETARIA_API_KEY_PREVIOUS="")
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)


# --- Pair-key gate ---------------------------------------------------------------


async def test_usage_events_key_unset_forbidden(client):
    """SECRETARIA_API_KEY is forced empty by conftest -> fail closed with 403."""
    resp = await client.post(
        "/internal/usage-events",
        headers={"X-Internal-Api-Key": "whatever"},
        json={
            "tenant_id": str(uuid4()),
            "feature": LIMIT_MESSAGES,
            "amount": 1,
            "event_id": "evt-unset",
        },
    )
    assert resp.status_code == 403


async def test_usage_events_wrong_key_unauthorized(client, monkeypatch):
    _set_pair_key(monkeypatch)
    resp = await client.post(
        "/internal/usage-events",
        headers={"X-Internal-Api-Key": "not-the-key"},
        json={
            "tenant_id": str(uuid4()),
            "feature": LIMIT_MESSAGES,
            "amount": 1,
            "event_id": "evt-wrong-key",
        },
    )
    assert resp.status_code == 401


# --- Happy path: increments entitlements.usage ------------------------------------


async def test_usage_events_happy_path_increments_usage(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]

    resp = await client.post(
        "/internal/usage-events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={
            "tenant_id": tenant_a_id,
            "feature": LIMIT_MESSAGES,
            "amount": 3,
            "event_id": "evt-happy-1",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"recorded": True}

    owner_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    ent = await client.get("/entitlements", headers=_bearer(owner_token))
    assert ent.status_code == 200, ent.text
    assert ent.json()["usage"][LIMIT_MESSAGES] == 3


# --- Idempotency: duplicate event_id does not double-count -------------------------


async def test_usage_events_idempotent_duplicate(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]

    body = {
        "tenant_id": tenant_a_id,
        "feature": LIMIT_REMINDERS,
        "amount": 5,
        "event_id": "evt-dup-1",
    }

    first = await client.post(
        "/internal/usage-events", headers={"X-Internal-Api-Key": "pair-key"}, json=body
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"recorded": True}

    second = await client.post(
        "/internal/usage-events", headers={"X-Internal-Api-Key": "pair-key"}, json=body
    )
    assert second.status_code == 200, second.text
    assert second.json() == {"recorded": False}

    owner_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    ent = await client.get("/entitlements", headers=_bearer(owner_token))
    assert ent.status_code == 200, ent.text
    # NOT double-incremented: still just the one application's worth (5), not 10.
    assert ent.json()["usage"][LIMIT_REMINDERS] == 5


# --- Validation ----------------------------------------------------------------------


async def test_usage_events_unknown_feature_422(client, monkeypatch):
    _set_pair_key(monkeypatch)
    resp = await client.post(
        "/internal/usage-events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={
            "tenant_id": str(uuid4()),
            "feature": "not_a_real_feature",
            "amount": 1,
            "event_id": "evt-bad-feature",
        },
    )
    assert resp.status_code == 422


async def test_usage_events_amount_zero_422(client, monkeypatch):
    _set_pair_key(monkeypatch)
    resp = await client.post(
        "/internal/usage-events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={
            "tenant_id": str(uuid4()),
            "feature": LIMIT_MESSAGES,
            "amount": 0,
            "event_id": "evt-zero-amount",
        },
    )
    assert resp.status_code == 422


# --- Upsert: tenant with no entitlement row yet gets one created -------------------


async def test_usage_events_creates_missing_entitlement_row(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = (await _tenant_ids(client, admin_token))[CLINIC_B]

    # Clinic B has no entitlement row yet in this test's fresh app instance.
    before = await client.get(
        f"/admin/tenants/{tenant_b_id}/entitlements", headers=_bearer(admin_token)
    )
    assert before.status_code == 200, before.text
    assert before.json()["usage"] == {}

    resp = await client.post(
        "/internal/usage-events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={
            "tenant_id": tenant_b_id,
            "feature": LIMIT_MESSAGES,
            "amount": 2,
            "event_id": "evt-upsert-1",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"recorded": True}

    after = await client.get(
        f"/admin/tenants/{tenant_b_id}/entitlements", headers=_bearer(admin_token)
    )
    assert after.status_code == 200, after.text
    assert after.json()["usage"][LIMIT_MESSAGES] == 2
