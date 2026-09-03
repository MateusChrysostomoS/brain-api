"""PreCheck billing vertical tests (precheck-billing round).

Ground truth: services/precheck_billing.py (quota_window, usage_summary),
services/billing.py (create_precheck_topup_checkout_session, upgrade_precheck_plan,
_apply_precheck_topup_checkout, the ensure_secretaria_provisioned gate — see
tests/test_signup.py for THAT one), api/internal_precheck.py, api/billing.py's three new
`/billing/precheck/*` routes, models/precheck_topup_credit.py.

Reuses the seeded `client` fixture (tests/test_rbac.py — Clinic A: plan="precheck",
active; Clinic B: no entitlement row) and the Stripe test doubles from tests/test_billing.py
(`_event`, `_post_webhook`, `_install_fake_stripe_httpx`, `_tenant_ids`). Webhook-internals
and service-layer tests use the bare `db_session` fixture directly (conftest.py),
mirroring tests/test_billing_phase1.py's convention for testing webhook apply logic
without going through the full HTTP + fake-Stripe-httpx round trip.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import brain_api.api.internal_precheck as internal_precheck_api
from brain_api.config import get_settings
from brain_api.models import Entitlement, PrecheckTopupCredit, Tenant, UsageEvent
from brain_api.services import billing as billing_service, catalog, precheck_billing
from tests.test_billing import _event, _install_fake_stripe_httpx, _post_webhook, _tenant_ids
from tests.test_rbac import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    CLINIC_A,
    CLINIC_B,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    OWNER_B_EMAIL,
    OWNER_B_PASSWORD,
    _bearer,
    _token,
)


# Avulso purchase bounds AS THE FAKE STRIPE SETTINGS SEE THEM: the top-up tests below run
# against `_install_fake_stripe_httpx`'s settings double, whose defaults mirror
# Settings.PRECHECK_TOPUP_MIN_QUANTITY / _MAX_QUANTITY. Asserted against these literals
# rather than the live `get_settings()` so a populated local .env can't move the boundary
# under the tests.
TOPUP_MIN_QUANTITY = 5
TOPUP_MAX_QUANTITY = 1000


def _set_precheck_key(monkeypatch, key: str = "precheck-pair-key") -> None:
    fake_settings = SimpleNamespace(PRECHECK_API_KEY=key, PRECHECK_API_KEY_PREVIOUS="")
    monkeypatch.setattr(internal_precheck_api, "get_settings", lambda: fake_settings)


# ===========================================================================================
# quota_window: pure function, no DB needed
# ===========================================================================================


def test_quota_window_prefers_stripe_period_when_not_ended():
    now = datetime(2026, 8, 15, tzinfo=UTC)
    ent = Entitlement(
        tenant_id=uuid4(),
        period_start=datetime(2026, 8, 1, tzinfo=UTC),
        period_end=datetime(2026, 9, 1, tzinfo=UTC),
    )
    start, end = precheck_billing.quota_window(ent, now)
    assert start == datetime(2026, 8, 1, tzinfo=UTC)
    assert end == datetime(2026, 9, 1, tzinfo=UTC)


def test_quota_window_falls_back_to_calendar_month_when_period_already_ended():
    now = datetime(2026, 8, 15, 12, 30, tzinfo=UTC)
    ent = Entitlement(
        tenant_id=uuid4(),
        period_start=datetime(2026, 6, 1, tzinfo=UTC),
        period_end=datetime(2026, 7, 1, tzinfo=UTC),  # already ended relative to `now`
    )
    start, end = precheck_billing.quota_window(ent, now)
    assert start == datetime(2026, 8, 1, tzinfo=UTC)
    assert end == datetime(2026, 9, 1, tzinfo=UTC)


def test_quota_window_falls_back_to_calendar_month_when_no_period_at_all():
    now = datetime(2026, 12, 15, tzinfo=UTC)
    ent = Entitlement(tenant_id=uuid4())  # period_start/period_end both unset (None)
    start, end = precheck_billing.quota_window(ent, now)
    assert start == datetime(2026, 12, 1, tzinfo=UTC)
    assert end == datetime(2027, 1, 1, tzinfo=UTC)  # year rollover


# ===========================================================================================
# usage_summary: needs a DB (usage_events / precheck_topup_credits queries)
# ===========================================================================================


async def test_usage_summary_ent_none_returns_zeroed_default(db_session):
    summary = await precheck_billing.usage_summary(db_session, None, datetime.now(UTC))
    assert summary.precheck_enabled is False
    assert summary.enforced is False
    assert summary.quota == 0
    assert summary.used == 0
    assert summary.topup_credits == 0
    assert summary.remaining == 0
    assert summary.allowed is True


async def test_usage_summary_unenforced_non_precheck_plan_is_allowed(db_session):
    tenant = Tenant(clinic_name="Unenforced Clinic")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(tenant_id=tenant.id, plan=catalog.PLAN_SECRETARIA_BASICO, status="active")
    db_session.add(ent)
    await db_session.commit()

    summary = await precheck_billing.usage_summary(db_session, ent, datetime.now(UTC))
    assert summary.enforced is False
    assert summary.quota == 0
    assert summary.allowed is True


async def test_usage_summary_counts_usage_in_window_and_excludes_outside(db_session):
    tenant = Tenant(clinic_name="Window Clinic")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan=catalog.PLAN_PRECHECK_BASIC,
        status="active",
        precheck_enabled=True,
    )
    db_session.add(ent)
    await db_session.commit()

    now = datetime.now(UTC)
    # Anchor both fixture timestamps to the WINDOW's own boundaries, not to `now` by a
    # fixed day-count offset -- `now - N days` can land in the PREVIOUS calendar month
    # whenever `now` falls early enough in the current one (e.g. the 1st/2nd), which
    # would silently misclassify the "in window" row as "outside" instead.
    window_start, _ = precheck_billing.quota_window(ent, now)
    db_session.add(
        UsageEvent(
            id="evt-in-window",
            tenant_id=tenant.id,
            feature=catalog.LIMIT_PRECHECK_CONSULTATIONS,
            amount=7,
            created_at=window_start + timedelta(hours=1),
        )
    )
    # One day BEFORE the window start always falls in the PREVIOUS calendar month.
    db_session.add(
        UsageEvent(
            id="evt-outside-window",
            tenant_id=tenant.id,
            feature=catalog.LIMIT_PRECHECK_CONSULTATIONS,
            amount=1000,
            created_at=window_start - timedelta(days=1),
        )
    )
    await db_session.commit()

    summary = await precheck_billing.usage_summary(db_session, ent, now)
    quota = get_settings().PRECHECK_BASIC_CONSULTATIONS_PER_MONTH
    assert summary.quota == quota
    assert summary.used == 7
    assert summary.remaining == quota - 7


async def test_usage_summary_credits_expiring_are_excluded_from_balance(db_session):
    tenant = Tenant(clinic_name="Credits Clinic")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan=catalog.PLAN_PRECHECK_BASIC,
        status="active",
        precheck_enabled=True,
    )
    db_session.add(ent)
    await db_session.commit()

    now = datetime.now(UTC)
    # Anchor `granted_at` to the WINDOW's own start, not to `now` by a fixed day-count
    # offset -- see the comment in test_usage_summary_counts_usage_in_window_and_
    # excludes_outside for why a plain `now - N days` is unsafe near a month boundary.
    window_start, _ = precheck_billing.quota_window(ent, now)
    db_session.add(
        PrecheckTopupCredit(
            tenant_id=tenant.id,
            amount=50,
            amount_total_cents=4999,
            currency="brl",
            stripe_checkout_session_id="cs_active_credit",
            expires_at=now + timedelta(days=5),
            granted_at=window_start + timedelta(hours=1),  # inside the current window
        )
    )
    db_session.add(
        PrecheckTopupCredit(
            tenant_id=tenant.id,
            amount=999,
            amount_total_cents=1,
            currency="brl",
            stripe_checkout_session_id="cs_expired_credit",
            expires_at=now - timedelta(days=1),  # already expired -> excluded from balance
            granted_at=window_start - timedelta(days=1),  # outside the window -> excluded from spend
        )
    )
    await db_session.commit()

    summary = await precheck_billing.usage_summary(db_session, ent, now)
    assert summary.topup_credits == 50  # the expired 999-credit pack is excluded
    # Spend is scoped by `granted_at` falling in the CURRENT window, independent of
    # expiry -- only the first pack (granted 1 day ago) counts.
    assert summary.spend_topup_cents == 4999
    assert summary.spend_topup_count == 1
    assert summary.spend_currency == "brl"


async def test_usage_summary_remaining_clamped_at_zero_when_over_quota(db_session):
    tenant = Tenant(clinic_name="Overused Clinic")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan=catalog.PLAN_PRECHECK_BASIC,
        status="active",
        precheck_enabled=True,
    )
    db_session.add(ent)
    await db_session.commit()

    now = datetime.now(UTC)
    quota = get_settings().PRECHECK_BASIC_CONSULTATIONS_PER_MONTH
    db_session.add(
        UsageEvent(
            id="evt-way-over",
            tenant_id=tenant.id,
            feature=catalog.LIMIT_PRECHECK_CONSULTATIONS,
            amount=quota + 500,
            created_at=now,
        )
    )
    await db_session.commit()

    summary = await precheck_billing.usage_summary(db_session, ent, now)
    assert summary.remaining == 0  # never negative
    assert summary.allowed is False


# ===========================================================================================
# Internal endpoints (/internal/precheck/*): auth, 404s, idempotency, quota decision
# ===========================================================================================


async def test_precheck_internal_usage_event_key_unset_403(client):
    """PRECHECK_API_KEY is unset by default (conftest never sets it) -> fail closed."""
    resp = await client.post(
        "/internal/precheck/usage-events",
        headers={"X-Internal-Api-Key": "whatever"},
        json={"tenant_id": str(uuid4()), "event_id": "pc-evt-unset"},
    )
    assert resp.status_code == 403


async def test_precheck_internal_usage_event_wrong_key_401(client, monkeypatch):
    _set_precheck_key(monkeypatch)
    resp = await client.post(
        "/internal/precheck/usage-events",
        headers={"X-Internal-Api-Key": "not-the-key"},
        json={"tenant_id": str(uuid4()), "event_id": "pc-evt-wrong-key"},
    )
    assert resp.status_code == 401


async def test_precheck_internal_usage_event_unknown_tenant_404(client, monkeypatch):
    _set_precheck_key(monkeypatch)
    resp = await client.post(
        "/internal/precheck/usage-events",
        headers={"X-Internal-Api-Key": "precheck-pair-key"},
        json={"tenant_id": str(uuid4()), "event_id": "pc-evt-unknown-tenant"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "entitlement_not_found"


async def test_precheck_internal_usage_event_idempotent_and_never_forwards_meter_event(
    client, monkeypatch
):
    """precheck_consultations is flat+quota billing, never metered — record_usage must
    never attempt a Stripe meter-event forward for this feature (services/usage.py's
    `_METER_EVENT_SETTINGS` has no entry for it)."""
    _set_precheck_key(monkeypatch)
    from brain_api.services import usage as usage_service

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("meter event must not be forwarded for precheck_consultations")

    monkeypatch.setattr(usage_service, "_forward_meter_event", fail_if_called)

    tenant_a_id = (await _tenant_ids(client))[CLINIC_A]
    body = {"tenant_id": tenant_a_id, "event_id": "pc-evt-idem-1"}

    first = await client.post(
        "/internal/precheck/usage-events",
        headers={"X-Internal-Api-Key": "precheck-pair-key"},
        json=body,
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"recorded": True}

    second = await client.post(
        "/internal/precheck/usage-events",
        headers={"X-Internal-Api-Key": "precheck-pair-key"},
        json=body,
    )
    assert second.status_code == 200, second.text
    assert second.json() == {"recorded": False}


async def test_precheck_internal_quota_unknown_tenant_404(client, monkeypatch):
    _set_precheck_key(monkeypatch)
    resp = await client.get(
        f"/internal/precheck/quota/{uuid4()}",
        headers={"X-Internal-Api-Key": "precheck-pair-key"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "entitlement_not_found"


async def test_precheck_internal_quota_allowed_under_quota(client, monkeypatch):
    _set_precheck_key(monkeypatch)
    tenant_a_id = (await _tenant_ids(client))[CLINIC_A]
    resp = await client.get(
        f"/internal/precheck/quota/{tenant_a_id}",
        headers={"X-Internal-Api-Key": "precheck-pair-key"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enforced"] is True
    assert body["allowed"] is True
    assert body["quota"] == get_settings().PRECHECK_BASIC_CONSULTATIONS_PER_MONTH
    assert body["used"] == 0
    assert body["remaining"] == body["quota"]


async def test_precheck_internal_quota_blocked_once_quota_exhausted(client, monkeypatch):
    _set_precheck_key(monkeypatch)
    tenant_a_id = (await _tenant_ids(client))[CLINIC_A]
    quota = get_settings().PRECHECK_BASIC_CONSULTATIONS_PER_MONTH

    # Recording is UNCONDITIONAL -- exhausting the quota exactly (amount == quota, still
    # <= the endpoint's own amount<=100 cap for the default 100-quota) still records.
    record = await client.post(
        "/internal/precheck/usage-events",
        headers={"X-Internal-Api-Key": "precheck-pair-key"},
        json={"tenant_id": tenant_a_id, "event_id": "pc-evt-exhaust", "amount": quota},
    )
    assert record.status_code == 200, record.text

    resp = await client.get(
        f"/internal/precheck/quota/{tenant_a_id}",
        headers={"X-Internal-Api-Key": "precheck-pair-key"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["used"] == quota
    assert body["remaining"] == 0
    assert body["allowed"] is False


async def test_precheck_internal_quota_unenforced_non_precheck_plan_is_allowed(client, monkeypatch):
    _set_precheck_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = (await _tenant_ids(client))[CLINIC_B]
    patch_resp = await client.patch(
        f"/admin/tenants/{tenant_b_id}/entitlements",
        headers=_bearer(admin_token),
        json={"plan": "secretaria_basico", "status": "active"},
    )
    assert patch_resp.status_code == 200, patch_resp.text

    resp = await client.get(
        f"/internal/precheck/quota/{tenant_b_id}",
        headers={"X-Internal-Api-Key": "precheck-pair-key"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enforced"] is False
    assert body["allowed"] is True
    assert body["quota"] == 0


# ===========================================================================================
# Top-up checkout: POST /billing/precheck/topup
# ===========================================================================================


async def test_precheck_topup_rejects_non_precheck_tenant(client):
    token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    resp = await client.post(
        "/billing/precheck/topup", headers=_bearer(token), json={"quantity": 10}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "not_precheck_plan"


async def test_precheck_topup_checkout_happy_path(client, monkeypatch):
    """The chosen quantity drives BOTH the line item (so Stripe charges quantity x unit
    price) and the metadata the webhook will grant from."""
    tenant_a_id = (await _tenant_ids(client))[CLINIC_A]
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": "https://checkout.stripe.test/topup"})

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/billing/precheck/topup", headers=_bearer(token), json={"quantity": 12}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": "https://checkout.stripe.test/topup"}

    data = captured["data"]
    assert data["mode"] == "payment"
    assert data["line_items[0][price]"] == "price_precheck_topup"
    assert data["line_items[0][quantity]"] == "12"
    assert data["metadata[kind]"] == "precheck_topup"
    assert data["metadata[tenant_id]"] == tenant_a_id
    assert data["metadata[quantity]"] == "12"
    # Quantity is fixed BEFORE the redirect: Stripe's hosted page must never re-ask for it.
    assert not any(k.startswith("line_items[0][adjustable_quantity]") for k in data)


async def test_precheck_topup_rejects_quantity_below_minimum(client, monkeypatch):
    """Below the minimum is refused SERVER-SIDE, whatever the frontend allows -- and
    without ever reaching Stripe (the fake httpx double would have recorded a call)."""
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": "https://checkout.stripe.test/topup"})

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/billing/precheck/topup",
        headers=_bearer(token),
        json={"quantity": TOPUP_MIN_QUANTITY - 1},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "quantity_below_minimum"
    assert "data" not in captured


async def test_precheck_topup_accepts_exactly_the_minimum(client, monkeypatch):
    """The minimum itself is INSIDE the allowed range (boundary, not an off-by-one)."""
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": "https://checkout.stripe.test/topup"})

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/billing/precheck/topup", headers=_bearer(token), json={"quantity": TOPUP_MIN_QUANTITY}
    )
    assert resp.status_code == 200, resp.text
    assert captured["data"]["line_items[0][quantity]"] == str(TOPUP_MIN_QUANTITY)


async def test_precheck_topup_rejects_quantity_above_maximum(client, monkeypatch):
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": "https://checkout.stripe.test/topup"})

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/billing/precheck/topup",
        headers=_bearer(token),
        json={"quantity": TOPUP_MAX_QUANTITY + 1},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == "quantity_above_maximum"
    assert "data" not in captured


# ===========================================================================================
# Webhook: precheck_topup grant (checkout.session.completed)
# ===========================================================================================


async def test_webhook_precheck_topup_grants_credit_and_replay_is_noop(db_session):
    """The grant is EXACTLY the purchased quantity (12 here, deliberately not a round
    default) -- never a fixed pack size, never a live-settings read."""
    tenant = Tenant(clinic_name="Topup Clinic")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan=catalog.PLAN_PRECHECK_BASIC,
        status="active",
        precheck_enabled=True,
        stripe_customer_id="cus_topup_1",
    )
    db_session.add(ent)
    await db_session.commit()

    obj = {
        "id": "cs_topup_1",
        "customer": "cus_topup_1",
        "amount_total": 1200,  # 12 x R$1,00
        "currency": "brl",
        "metadata": {"kind": "precheck_topup", "tenant_id": str(tenant.id), "quantity": "12"},
    }
    applied = await billing_service.apply_stripe_event(
        db_session, "evt_topup_1", "checkout.session.completed", obj
    )
    assert applied is True

    credits = (
        await db_session.scalars(
            select(PrecheckTopupCredit).where(PrecheckTopupCredit.tenant_id == tenant.id)
        )
    ).all()
    assert len(credits) == 1
    credit = credits[0]
    assert credit.amount == 12
    assert credit.amount_total_cents == 1200
    assert credit.currency == "brl"
    assert credit.stripe_checkout_session_id == "cs_topup_1"
    assert credit.expires_at is not None

    # Replay of the SAME event id -- processed_stripe_events dedup makes it a pure no-op
    # (apply_stripe_event returns False, nothing re-applied).
    replay = await billing_service.apply_stripe_event(
        db_session, "evt_topup_1", "checkout.session.completed", obj
    )
    assert replay is False
    credits_after_replay = (
        await db_session.scalars(
            select(PrecheckTopupCredit).where(PrecheckTopupCredit.tenant_id == tenant.id)
        )
    ).all()
    assert len(credits_after_replay) == 1


async def test_webhook_precheck_topup_duplicate_session_id_is_noop(db_session):
    """Belt-and-braces guard: a DIFFERENT event.id completing the SAME Checkout Session
    (e.g. a Stripe dashboard resend) must not grant a second credit row -- the unique
    constraint on stripe_checkout_session_id is caught (SAVEPOINT) and treated as a
    no-op replay, while the EVENT itself is still marked processed."""
    tenant = Tenant(clinic_name="Topup Dup Clinic")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan=catalog.PLAN_PRECHECK_BASIC,
        status="active",
        precheck_enabled=True,
        stripe_customer_id="cus_topup_dup",
    )
    db_session.add(ent)
    await db_session.commit()

    obj = {
        "id": "cs_topup_dup",
        "customer": "cus_topup_dup",
        "amount_total": 2000,  # 20 x R$1,00
        "currency": "brl",
        "metadata": {"kind": "precheck_topup", "tenant_id": str(tenant.id), "quantity": "20"},
    }
    first = await billing_service.apply_stripe_event(
        db_session, "evt_topup_dup_1", "checkout.session.completed", obj
    )
    assert first is True

    # A DIFFERENT event id, SAME checkout session id -- processed_stripe_events does NOT
    # dedupe this (different event id), so the grant logic runs again.
    second = await billing_service.apply_stripe_event(
        db_session, "evt_topup_dup_2", "checkout.session.completed", obj
    )
    assert second is True  # the EVENT still gets marked processed -- only the grant no-ops

    credits = (
        await db_session.scalars(
            select(PrecheckTopupCredit).where(PrecheckTopupCredit.tenant_id == tenant.id)
        )
    ).all()
    assert len(credits) == 1


# ===========================================================================================
# Upgrade: POST /billing/precheck/upgrade
# ===========================================================================================


async def test_precheck_upgrade_invalid_target_422(client):
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/billing/precheck/upgrade", headers=_bearer(token), json={"plan": "secretaria_basico"}
    )
    assert resp.status_code == 422


async def test_precheck_upgrade_not_precheck_plan_409(client):
    token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    resp = await client.post(
        "/billing/precheck/upgrade", headers=_bearer(token), json={"plan": "precheck_advanced"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "not_precheck_plan"


async def test_precheck_upgrade_already_on_plan_409(client):
    """Clinic A is seeded with plan="precheck" -- the LEGACY alias for precheck_basic --
    so requesting "precheck_basic" must resolve as the SAME (canonical) plan."""
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/billing/precheck/upgrade", headers=_bearer(token), json={"plan": "precheck_basic"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "already_on_plan"


async def test_precheck_upgrade_no_active_subscription_409(client):
    """Clinic A's seeded entitlement carries no stripe_subscription_id at all."""
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/billing/precheck/upgrade", headers=_bearer(token), json={"plan": "precheck_advanced"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "no_active_subscription"


async def test_precheck_upgrade_swaps_price_and_updates_entitlement(db_session, monkeypatch):
    tenant = Tenant(clinic_name="Upgrade Clinic")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan=catalog.PLAN_PRECHECK_BASIC,
        status="active",
        precheck_enabled=True,
        stripe_customer_id="cus_upgrade",
        stripe_subscription_id="sub_upgrade",
    )
    db_session.add(ent)
    await db_session.commit()

    calls: dict = {}

    async def fake_stripe_get(path):
        calls["get_path"] = path
        return {
            "id": "sub_upgrade",
            "items": {"data": [{"id": "si_current", "price": {"id": "price_precheck"}}]},
        }

    async def fake_stripe_post(path, data, *, idempotency_key=None):
        calls["post_path"] = path
        calls["post_data"] = data
        return {"id": "sub_upgrade", "status": "active"}

    monkeypatch.setattr(billing_service, "_stripe_get", fake_stripe_get)
    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_post)

    summary = await billing_service.upgrade_precheck_plan(
        db_session, tenant.id, catalog.PLAN_PRECHECK_ADVANCED
    )

    assert calls["get_path"] == "/v1/subscriptions/sub_upgrade"
    assert calls["post_path"] == "/v1/subscriptions/sub_upgrade"
    assert calls["post_data"]["items[0][id]"] == "si_current"
    assert calls["post_data"]["items[0][price]"] == "price_precheck_advanced"
    assert calls["post_data"]["proration_behavior"] == "create_prorations"

    await db_session.refresh(ent)
    assert ent.plan == catalog.PLAN_PRECHECK_ADVANCED
    advanced_quota = get_settings().PRECHECK_ADVANCED_CONSULTATIONS_PER_MONTH
    assert ent.limits[catalog.LIMIT_PRECHECK_CONSULTATIONS] == advanced_quota

    # The returned summary reflects the NEW plan immediately (optimistic local update).
    assert summary.plan == catalog.PLAN_PRECHECK_ADVANCED
    assert summary.quota == advanced_quota


async def test_precheck_upgrade_swaps_into_the_start_tier(db_session, monkeypatch):
    """The swap target is validated against catalog.PRECHECK_TIER_PLAN_IDS, not against a
    hardcoded Basic/Advanced pair -- so the entry tier added in the 2026-09-03 three-tier
    round is a legal target, and in BOTH directions (this one swaps DOWN, from Advanced).
    Written against the newest tier on purpose: the old two-id membership check answered
    422 invalid_precheck_plan here."""
    tenant = Tenant(clinic_name="Downgrade Clinic")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan=catalog.PLAN_PRECHECK_ADVANCED,
        status="active",
        precheck_enabled=True,
        stripe_customer_id="cus_start",
        stripe_subscription_id="sub_start",
    )
    db_session.add(ent)
    await db_session.commit()

    calls: dict = {}

    async def fake_stripe_get(path):
        return {
            "id": "sub_start",
            "items": {
                "data": [{"id": "si_adv", "price": {"id": "price_precheck_advanced"}}]
            },
        }

    async def fake_stripe_post(path, data, *, idempotency_key=None):
        calls["post_data"] = data
        return {"id": "sub_start", "status": "active"}

    monkeypatch.setattr(billing_service, "_stripe_get", fake_stripe_get)
    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_post)

    summary = await billing_service.upgrade_precheck_plan(
        db_session, tenant.id, catalog.PLAN_PRECHECK_START
    )

    assert calls["post_data"]["items[0][id]"] == "si_adv"
    assert calls["post_data"]["items[0][price]"] == "price_precheck_start"

    await db_session.refresh(ent)
    start_quota = get_settings().PRECHECK_START_CONSULTATIONS_PER_MONTH
    assert ent.plan == catalog.PLAN_PRECHECK_START
    assert ent.limits[catalog.LIMIT_PRECHECK_CONSULTATIONS] == start_quota
    assert summary.plan == catalog.PLAN_PRECHECK_START
    assert summary.quota == start_quota


async def test_precheck_upgrade_subscription_price_mismatch_409(db_session, monkeypatch):
    """Defensive: the live subscription carries no item at the CURRENT plan's price at
    all -- nothing to identify as "the item to swap"."""
    tenant = Tenant(clinic_name="Mismatch Clinic")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan=catalog.PLAN_PRECHECK_BASIC,
        status="active",
        precheck_enabled=True,
        stripe_subscription_id="sub_mismatch",
    )
    db_session.add(ent)
    await db_session.commit()

    async def fake_stripe_get(path):
        return {
            "id": "sub_mismatch",
            "items": {"data": [{"id": "si_x", "price": {"id": "price_unrelated"}}]},
        }

    monkeypatch.setattr(billing_service, "_stripe_get", fake_stripe_get)

    with pytest.raises(HTTPException) as exc_info:
        await billing_service.upgrade_precheck_plan(
            db_session, tenant.id, catalog.PLAN_PRECHECK_ADVANCED
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "subscription_price_mismatch"


# ===========================================================================================
# GET /billing/precheck/usage
# ===========================================================================================


async def test_precheck_usage_endpoint_shape_for_precheck_tenant(client):
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/billing/precheck/usage", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan"] == catalog.PLAN_PRECHECK_BASIC  # canonicalized from the "precheck" alias
    assert body["precheck_enabled"] is True
    assert body["enforced"] is True
    quota = get_settings().PRECHECK_BASIC_CONSULTATIONS_PER_MONTH
    assert body["quota"] == quota
    assert body["used"] == 0
    assert body["remaining"] == quota
    assert body["topup_credits"] == 0
    assert body["topup_expires_at"] is None
    assert "window_start" in body
    assert "window_end" in body
    assert body["spend"] == {"topup_cents": 0, "topup_count": 0, "currency": None}


async def test_precheck_usage_endpoint_zeros_for_non_precheck_tenant(client):
    """Clinic B has no entitlement row at all -- still 200, not 404, with everything
    zeroed/false so the frontend can hide the PreCheck section."""
    token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    resp = await client.get("/billing/precheck/usage", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["precheck_enabled"] is False
    assert body["enforced"] is False
    assert body["quota"] == 0
    assert body["used"] == 0
    assert body["remaining"] == 0
    assert body["topup_credits"] == 0
    assert body["topup_expires_at"] is None


async def test_precheck_usage_requires_tenant_scoped_token(client):
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    admin_resp = await client.get("/billing/precheck/usage", headers=_bearer(admin_token))
    assert admin_resp.status_code == 409

    noauth_resp = await client.get("/billing/precheck/usage")
    assert noauth_resp.status_code == 401
