"""Billing Phase 1 / fully-metered vertical tests (CONTRACT_onboarding_v1.md §9): the
three metered companion Checkout line items (no anchor price, ALL THREE required to
waive the plan's own price) + trial period on BOTH checkout builders, `services.billing.
harden_charge`, the `customer.subscription.trial_will_end` -> scheduled cancellation
handler (row-locked re-read + a LIVE Stripe GET verify, scoped to secretarIA-bearing
plans only), the resubscription marker-reset guard, and the `services.usage` Stripe
meter-event forward for `billable_patients`, `active_professionals`, and `reminders`.

Ground truth: services/billing.py (`_append_checkout_line_items`, `_apply_trial`,
`validate_selection`'s fully-metered waiver (all three companions required),
`_parse_price_map`'s three-companion (+ legacy `{plan}_metered`) acceptance,
`_state_from_subscription`'s companion-as-plan-evidence resolution, `apply_stripe_event`'s
`trial_will_end` branch and `_reset_markers_if_subscription_changed`, `harden_charge`),
services/usage.py (`_forward_meter_event`). Reuses the Stripe test doubles from
tests/test_billing.py (real HMAC-signed webhook helper, the fake-httpx-client
`_install_fake_stripe_httpx` pattern) and the seeded client fixture from
tests/test_rbac.py.
"""

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from brain_api.models import Entitlement, ProcessedStripeEvent, Tenant
from brain_api.services import billing as billing_service, catalog, usage as usage_module
from tests.test_billing import (
    _admin_entitlements,
    _event,
    _install_fake_stripe_httpx,
    _post_webhook,
    _tenant_ids as _billing_tenant_ids,
)
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
from tests.test_signup import _create_intent
from tests.test_usage_events import _set_pair_key, _tenant_ids

# ===========================================================================================
# Checkout: metered line item + trial (both builders)
# ===========================================================================================


_METERED_PRICE_MAP = (
    '{"secretaria_basico": "price_ferro", '
    '"secretaria_basico_metered_patients": "price_ferro_meter_patients", '
    '"secretaria_basico_metered_professionals": "price_ferro_meter_professionals", '
    '"secretaria_basico_metered_reminders": "price_ferro_meter_reminders"}'
)

#: A plan with ONLY the three metered companions configured -- NO direct/anchor price at
#: all (the actual deployed secretaria_basico shape per this round's business decision).
_FULLY_METERED_PRICE_MAP = (
    '{"secretaria_basico_metered_patients": "price_ferro_meter_patients", '
    '"secretaria_basico_metered_professionals": "price_ferro_meter_professionals", '
    '"secretaria_basico_metered_reminders": "price_ferro_meter_reminders"}'
)


def _extended_fake_settings(**overrides) -> SimpleNamespace:
    base = dict(
        STRIPE_SECRET_KEY="sk_test",
        STRIPE_API_BASE="https://api.stripe.test",
        STRIPE_TIMEOUT_SECONDS=15.0,
        STRIPE_PRICE_MAP=_METERED_PRICE_MAP,
        STRIPE_CHECKOUT_SUCCESS_URL="http://localhost:3000/app?checkout=success",
        STRIPE_CHECKOUT_CANCEL_URL="http://localhost:3000/app?checkout=cancelled",
        STRIPE_PORTAL_RETURN_URL="http://localhost:3000/app",
        STRIPE_TRIAL_PERIOD_DAYS=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_existing_tenant_checkout_appends_all_three_metered_companions_and_trial(
    client, monkeypatch
):
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"url": "https://checkout.stripe.test/metered"}
    )
    monkeypatch.setattr(
        billing_service,
        "get_settings",
        lambda: _extended_fake_settings(STRIPE_TRIAL_PERIOD_DAYS=75),
    )

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/billing/checkout", headers=_bearer(token), json={"plan": "secretaria_basico"}
    )
    assert resp.status_code == 200, resp.text

    data = captured["data"]
    assert data["line_items[0][price]"] == "price_ferro"
    assert data["line_items[0][quantity]"] == "1"
    assert data["line_items[1][price]"] == "price_ferro_meter_patients"
    # Stripe rejects a quantity on a metered price -- must NOT be present.
    assert "line_items[1][quantity]" not in data
    assert data["line_items[2][price]"] == "price_ferro_meter_professionals"
    assert "line_items[2][quantity]" not in data
    assert data["line_items[3][price]"] == "price_ferro_meter_reminders"
    assert "line_items[3][quantity]" not in data
    assert data["subscription_data[trial_period_days]"] == "75"


async def test_existing_tenant_checkout_no_metered_price_no_extra_line_item(client, monkeypatch):
    """Without a `{plan}_metered` STRIPE_PRICE_MAP key and with STRIPE_TRIAL_PERIOD_DAYS
    at its default (0/off), neither the extra line item nor the trial param appears —
    proves both features are opt-in and backward compatible with the existing checkout
    flow (tests/test_billing.py's `test_checkout_happy_path` already asserts the base
    shape is otherwise unchanged). `_install_fake_stripe_httpx`'s own fake settings carry
    STRIPE_TRIAL_PERIOD_DAYS=0 and the baseline PRICE_MAP_JSON (no metered key) as-is."""
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": "https://checkout.stripe.test/plain"})

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/billing/checkout", headers=_bearer(token), json={"plan": "secretaria_basico"}
    )
    assert resp.status_code == 200, resp.text
    data = captured["data"]
    assert "line_items[1][price]" not in data
    assert "subscription_data[trial_period_days]" not in data


async def test_signup_checkout_appends_all_three_metered_companions_and_trial(client, monkeypatch):
    """The SAME fully-metered behavior on the cold-signup checkout builder
    (services.signup.create_checkout_session_for_intent), which reuses
    billing._append_checkout_line_items/_apply_trial."""
    intent_id = await _create_intent(
        client, email="metered.signup@example.com", catalog_ids=["secretaria_basico"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_metered_signup", "url": "https://checkout.stripe.test/ms"}
    )
    monkeypatch.setattr(
        billing_service,
        "get_settings",
        lambda: _extended_fake_settings(STRIPE_TRIAL_PERIOD_DAYS=30),
    )

    resp = await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    assert resp.status_code == 200, resp.text
    data = captured["data"]
    assert data["line_items[0][price]"] == "price_ferro"
    assert data["line_items[1][price]"] == "price_ferro_meter_patients"
    assert "line_items[1][quantity]" not in data
    assert data["line_items[2][price]"] == "price_ferro_meter_professionals"
    assert "line_items[2][quantity]" not in data
    assert data["line_items[3][price]"] == "price_ferro_meter_reminders"
    assert "line_items[3][quantity]" not in data
    assert data["subscription_data[trial_period_days]"] == "30"


def test_price_map_accepts_all_three_metered_companion_keys():
    """`_parse_price_map` must accept `{plan_id}_metered_patients`,
    `{plan_id}_metered_professionals`, AND `{plan_id}_metered_reminders` without
    rejecting the whole map as 'unknown catalog ids' -- none of the three is a catalog
    id, just three recognized suffixes."""
    parsed = billing_service._parse_price_map(
        '{"secretaria_basico": "price_a", '
        '"secretaria_basico_metered_patients": "price_b", '
        '"secretaria_basico_metered_professionals": "price_c", '
        '"secretaria_basico_metered_reminders": "price_d"}'
    )
    assert parsed == {
        "secretaria_basico": "price_a",
        "secretaria_basico_metered_patients": "price_b",
        "secretaria_basico_metered_professionals": "price_c",
        "secretaria_basico_metered_reminders": "price_d",
    }


def test_price_map_still_accepts_legacy_single_metered_key():
    """The SUPERSEDED single-companion `{plan_id}_metered` key (pre-fully-metered
    model) must still parse without error -- graceful degradation so a deployed map not
    yet cleaned up cannot blow up every billing call -- even though no code path (line
    items, plan resolution) reads it anymore."""
    parsed = billing_service._parse_price_map(
        '{"secretaria_basico": "price_a", "secretaria_basico_metered": "price_legacy"}'
    )
    assert parsed == {
        "secretaria_basico": "price_a",
        "secretaria_basico_metered": "price_legacy",
    }


def test_price_map_still_rejects_genuinely_unknown_keys():
    with pytest.raises(ValueError):
        billing_service._parse_price_map('{"totally_bogus_key": "price_x"}')


def test_append_checkout_line_items_fully_metered_plan_omits_blank_plan_line_item(
    monkeypatch,
):
    """A plan with NO direct price (fully metered) must not get a blank-price line item
    for itself -- only the configured companions appear, in patients-then-professionals-
    then-reminders order, none carrying a `quantity` key."""
    monkeypatch.setattr(
        billing_service,
        "get_settings",
        lambda: _extended_fake_settings(STRIPE_PRICE_MAP=_FULLY_METERED_PRICE_MAP),
    )
    data: dict[str, str] = {}
    selection = billing_service.CheckoutSelection(plan_id="secretaria_basico", addon_ids=())
    billing_service._append_checkout_line_items(data, selection)
    assert data == {
        "line_items[0][price]": "price_ferro_meter_patients",
        "line_items[1][price]": "price_ferro_meter_professionals",
        "line_items[2][price]": "price_ferro_meter_reminders",
    }


def test_validate_selection_fully_metered_plan_passes_without_direct_price(monkeypatch):
    """secretaria_basico with ONLY the three metered companions configured (no flat/
    anchor price) must validate -- a fully configured companion trio is proof enough the
    plan is sellable (CONTRACT_onboarding_v1.md §9)."""
    monkeypatch.setattr(
        billing_service,
        "get_settings",
        lambda: _extended_fake_settings(STRIPE_PRICE_MAP=_FULLY_METERED_PRICE_MAP),
    )
    selection = billing_service.validate_selection("secretaria_basico", None)
    assert selection.plan_id == "secretaria_basico"
    assert selection.addon_ids == ()


def test_validate_selection_neither_direct_nor_companion_raises_price_not_configured(
    monkeypatch,
):
    monkeypatch.setattr(
        billing_service, "get_settings", lambda: _extended_fake_settings(STRIPE_PRICE_MAP="{}")
    )
    with pytest.raises(HTTPException) as exc_info:
        billing_service.validate_selection("secretaria_basico", None)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "price_not_configured:secretaria_basico"


def test_validate_selection_professionals_only_no_anchor_raises(monkeypatch):
    """A companion set missing `_metered_patients` and `_metered_reminders` (and no
    anchor either) must NOT waive the price requirement -- checking out here would
    create a subscription silently missing two meters, so those usage dimensions would
    accrue locally but never actually get invoiced (silent under-billing)."""
    monkeypatch.setattr(
        billing_service,
        "get_settings",
        lambda: _extended_fake_settings(
            STRIPE_PRICE_MAP=(
                '{"secretaria_basico_metered_professionals": "price_ferro_meter_professionals"}'
            )
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        billing_service.validate_selection("secretaria_basico", None)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "price_not_configured:secretaria_basico"


def test_validate_selection_patients_only_no_anchor_raises(monkeypatch):
    """Symmetric case: only the patients companion configured, professionals AND
    reminders missing, no anchor -- same 503, same reasoning (§13.3)."""
    monkeypatch.setattr(
        billing_service,
        "get_settings",
        lambda: _extended_fake_settings(
            STRIPE_PRICE_MAP=('{"secretaria_basico_metered_patients": "price_ferro_meter_patients"}')
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        billing_service.validate_selection("secretaria_basico", None)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "price_not_configured:secretaria_basico"


def test_validate_selection_two_of_three_companions_missing_reminders_raises(monkeypatch):
    """NEW boundary introduced by the third companion: patients + professionals BOTH
    configured but reminders missing (and no anchor) must still 503 -- the waiver
    requires ALL THREE, not 'most of them', or the reminders usage dimension would
    accrue locally but never get invoiced (same silent-under-billing reasoning as the
    single-companion cases above, now generalized to 'any 2 of 3')."""
    monkeypatch.setattr(
        billing_service,
        "get_settings",
        lambda: _extended_fake_settings(
            STRIPE_PRICE_MAP=(
                '{"secretaria_basico_metered_patients": "price_ferro_meter_patients", '
                '"secretaria_basico_metered_professionals": "price_ferro_meter_professionals"}'
            )
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        billing_service.validate_selection("secretaria_basico", None)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "price_not_configured:secretaria_basico"


# ===========================================================================================
# Webhook: fully-metered subscription (companions-only items) still resolves the plan
# ===========================================================================================


async def test_subscription_updated_fully_metered_companions_only_resolves_plan(
    client, monkeypatch
):
    """A fully-metered secretaria_basico subscription has NO anchor/flat price item at
    all -- only the three companion prices. `_state_from_subscription` must still
    resolve the plan from any companion (CONTRACT_onboarding_v1.md §9 integration fix)
    so the entitlement's plan/products/limits get set instead of logging
    stripe_subscription_no_known_plan forever."""
    monkeypatch.setattr(billing_service, "get_settings", lambda: _extended_fake_settings())
    tenant_b = (await _billing_tenant_ids(client))[CLINIC_B]
    now = int(time.time())
    obj = {
        "id": "sub_fully_metered",
        "customer": "cus_fm",
        "status": "active",
        "current_period_start": now,
        "current_period_end": now + 30 * 86400,
        "items": {
            "data": [
                {"price": {"id": "price_ferro_meter_patients"}, "quantity": 1},
                {"price": {"id": "price_ferro_meter_professionals"}, "quantity": 1},
                {"price": {"id": "price_ferro_meter_reminders"}, "quantity": 1},
            ]
        },
        "metadata": {"tenant_id": tenant_b},
    }
    resp = await _post_webhook(
        client, _event("evt_fully_metered", "customer.subscription.updated", obj)
    )
    assert resp.status_code == 200, resp.text

    ent = await _admin_entitlements(client, tenant_b)
    assert ent["plan"] == "secretaria_basico"
    assert ent["secretaria_enabled"] is True
    assert ent["precheck_enabled"] is False
    assert ent["status"] == "active"
    assert ent["limits"]["professionals"] == 1  # base grant only, no addon scaling
    # Companion items are metering evidence only -- never treated as add-ons.
    assert ent["addons"]["multi_professional"] is False


async def test_subscription_with_anchor_and_companions_resolves_plan_no_bogus_addons(
    client, monkeypatch
):
    """A plan that has BOTH a direct/anchor price item AND the three metered companion
    items (not the pure fully-metered case) must still resolve correctly from the
    anchor, and the companion items must NOT be misread as add-ons."""
    monkeypatch.setattr(billing_service, "get_settings", lambda: _extended_fake_settings())
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b = (await _tenant_ids(client, admin_token))[CLINIC_B]
    now = int(time.time())
    obj = {
        "id": "sub_anchor_plus_companions",
        "customer": "cus_apc",
        "status": "active",
        "current_period_start": now,
        "current_period_end": now + 30 * 86400,
        "items": {
            "data": [
                {"price": {"id": "price_ferro"}, "quantity": 1},
                {"price": {"id": "price_ferro_meter_patients"}, "quantity": 1},
                {"price": {"id": "price_ferro_meter_professionals"}, "quantity": 1},
                {"price": {"id": "price_ferro_meter_reminders"}, "quantity": 1},
            ]
        },
        "metadata": {"tenant_id": tenant_b},
    }
    resp = await _post_webhook(
        client, _event("evt_anchor_plus_companions", "customer.subscription.updated", obj)
    )
    assert resp.status_code == 200, resp.text

    ent = await _admin_entitlements(client, tenant_b)
    assert ent["plan"] == "secretaria_basico"
    assert ent["limits"]["professionals"] == 1  # base grant only -- no bogus addon scaling
    assert ent["addons"]["multi_professional"] is False


# ===========================================================================================
# harden_charge
# ===========================================================================================


async def test_harden_charge_noop_when_not_trialing(db_session):
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        Entitlement(tenant_id=tenant.id, status="active", stripe_subscription_id="sub_1")
    )
    await db_session.flush()

    assert await billing_service.harden_charge(db_session, tenant) is False


async def test_harden_charge_noop_without_subscription_id(db_session):
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(Entitlement(tenant_id=tenant.id, status="trialing", stripe_subscription_id=None))
    await db_session.flush()

    assert await billing_service.harden_charge(db_session, tenant) is False


async def test_harden_charge_noop_when_no_entitlement_row(db_session):
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()

    assert await billing_service.harden_charge(db_session, tenant) is False


async def test_harden_charge_success_then_idempotent(db_session, monkeypatch):
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    # Starts with a cancellation ALREADY scheduled (as trial_will_end would have left
    # it) to prove harden_charge clears it, not just that it starts/stays None.
    ent = Entitlement(
        tenant_id=tenant.id,
        status="trialing",
        stripe_subscription_id="sub_123",
        cancel_scheduled_at=datetime.now(UTC),
    )
    db_session.add(ent)
    await db_session.flush()

    captured: dict = {}

    async def fake_stripe_post(path, data):
        captured["path"] = path
        captured["data"] = data
        return {}

    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_post)

    result = await billing_service.harden_charge(db_session, tenant)
    assert result is True
    assert captured["path"] == "/v1/subscriptions/sub_123"
    assert captured["data"] == {
        "trial_end": "now",
        "proration_behavior": "none",
        "cancel_at": "",
    }
    assert ent.charge_hardened_at is not None
    assert ent.cancel_scheduled_at is None

    # Idempotent: a second call is a pure no-op and does NOT call Stripe again.
    captured.clear()
    result2 = await billing_service.harden_charge(db_session, tenant)
    assert result2 is False
    assert captured == {}


async def test_harden_charge_fail_soft_on_stripe_error(db_session, monkeypatch):
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(tenant_id=tenant.id, status="trialing", stripe_subscription_id="sub_err")
    db_session.add(ent)
    await db_session.flush()

    async def failing_stripe_post(path, data):
        raise HTTPException(502, "stripe_error")

    monkeypatch.setattr(billing_service, "_stripe_post", failing_stripe_post)

    result = await billing_service.harden_charge(db_session, tenant)
    assert result is False
    assert ent.charge_hardened_at is None


# ===========================================================================================
# trial_will_end -> scheduled Stripe cancellation (CONTRACTS.md §13.6)
# ===========================================================================================


def _trial_will_end_obj(sub_id: str, customer_id: str, trial_end_ts: int, status: str) -> dict:
    return {
        "id": sub_id,
        "customer": customer_id,
        "status": status,
        "trial_end": trial_end_ts,
    }


def _live_subscription(status: str, trial_end_ts: int):
    """A fake `_stripe_get` returning a live subscription snapshot (FIX 1b)."""

    async def fake_stripe_get(path):
        return {"status": status, "trial_end": trial_end_ts}

    return fake_stripe_get


async def test_trial_will_end_schedules_with_live_trial_end(db_session, monkeypatch):
    """Schedules using the LIVE trial_end (from `_stripe_get`), NOT the event's own --
    deliberately different here to prove which one wins (FIX 1b)."""
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan="secretaria_ferro",
        status="trialing",
        stripe_customer_id="cus_twe1",
        stripe_subscription_id="sub_twe1",
    )
    db_session.add(ent)
    await db_session.commit()

    captured: dict = {}

    async def fake_stripe_post(path, data):
        captured["path"] = path
        captured["data"] = data
        return {}

    event_trial_end = int(time.time()) + 3 * 86400
    live_trial_end = int(time.time()) + 5 * 86400  # deliberately DIFFERENT from the event's
    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_post)
    monkeypatch.setattr(
        billing_service, "_stripe_get", _live_subscription("trialing", live_trial_end)
    )

    applied = await billing_service.apply_stripe_event(
        db_session,
        "evt_twe_1",
        "customer.subscription.trial_will_end",
        _trial_will_end_obj("sub_twe1", "cus_twe1", event_trial_end, "trialing"),
    )
    assert applied is True
    assert captured["path"] == "/v1/subscriptions/sub_twe1"
    assert captured["data"] == {
        "cancel_at": str(live_trial_end),
        "proration_behavior": "none",
    }
    assert ent.cancel_scheduled_at is not None


async def test_trial_will_end_live_status_not_trialing_skips(db_session, monkeypatch):
    """FIX 1b: the LIVE subscription is the authority. Even though the cheap
    short-circuit + locked local guards all pass, a live status that is no longer
    "trialing" (e.g. harden_charge's Stripe call landed but its OWN commit hadn't yet
    when this event applied) must skip scheduling entirely."""
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan="secretaria_ferro",
        status="trialing",
        stripe_customer_id="cus_twe6",
        stripe_subscription_id="sub_twe6",
    )
    db_session.add(ent)
    await db_session.commit()

    called = {"n": 0}

    async def fake_stripe_post(path, data):
        called["n"] += 1
        return {}

    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_post)
    monkeypatch.setattr(
        billing_service,
        "_stripe_get",
        _live_subscription("active", int(time.time()) + 3 * 86400),
    )

    applied = await billing_service.apply_stripe_event(
        db_session,
        "evt_twe_6",
        "customer.subscription.trial_will_end",
        _trial_will_end_obj("sub_twe6", "cus_twe6", int(time.time()) + 3 * 86400, "trialing"),
    )
    assert applied is True
    assert called["n"] == 0
    assert ent.cancel_scheduled_at is None


async def test_trial_will_end_live_trial_end_too_soon_skips(db_session, monkeypatch):
    """The live trial_end must be more than an hour out; one ~now (about to end
    anyway regardless of this handler) must not schedule a racy cancellation."""
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan="secretaria_ferro",
        status="trialing",
        stripe_customer_id="cus_twe7",
        stripe_subscription_id="sub_twe7",
    )
    db_session.add(ent)
    await db_session.commit()

    called = {"n": 0}

    async def fake_stripe_post(path, data):
        called["n"] += 1
        return {}

    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_post)
    monkeypatch.setattr(
        billing_service, "_stripe_get", _live_subscription("trialing", int(time.time()) + 60)
    )

    applied = await billing_service.apply_stripe_event(
        db_session,
        "evt_twe_7",
        "customer.subscription.trial_will_end",
        _trial_will_end_obj("sub_twe7", "cus_twe7", int(time.time()) + 3 * 86400, "trialing"),
    )
    assert applied is True
    assert called["n"] == 0
    assert ent.cancel_scheduled_at is None


async def test_trial_will_end_skipped_when_already_hardened(db_session, monkeypatch):
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan="secretaria_ferro",
        status="trialing",
        stripe_customer_id="cus_twe2",
        stripe_subscription_id="sub_twe2",
        charge_hardened_at=datetime.now(UTC),
    )
    db_session.add(ent)
    await db_session.commit()

    called = {"n": 0}

    async def fake_stripe_post(path, data):
        called["n"] += 1
        return {}

    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_post)
    # Deliberately NOT mocking _stripe_get: the guard fails BEFORE the live refetch, so
    # it must never even be called -- an unmocked call would 503 (STRIPE_SECRET_KEY is
    # unset in tests), which would fail this test if the guard didn't short-circuit.

    applied = await billing_service.apply_stripe_event(
        db_session,
        "evt_twe_2",
        "customer.subscription.trial_will_end",
        _trial_will_end_obj("sub_twe2", "cus_twe2", int(time.time()) + 3 * 86400, "trialing"),
    )
    assert applied is True
    assert called["n"] == 0
    assert ent.cancel_scheduled_at is None


async def test_trial_will_end_skipped_when_already_scheduled(db_session, monkeypatch):
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    already_scheduled = datetime.now(UTC)
    ent = Entitlement(
        tenant_id=tenant.id,
        plan="secretaria_ferro",
        status="trialing",
        stripe_customer_id="cus_twe3",
        stripe_subscription_id="sub_twe3",
        cancel_scheduled_at=already_scheduled,
    )
    db_session.add(ent)
    await db_session.commit()

    called = {"n": 0}

    async def fake_stripe_post(path, data):
        called["n"] += 1
        return {}

    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_post)
    # Same reasoning as above: NOT mocking _stripe_get -- must never be reached.

    applied = await billing_service.apply_stripe_event(
        db_session,
        "evt_twe_3",
        "customer.subscription.trial_will_end",
        _trial_will_end_obj("sub_twe3", "cus_twe3", int(time.time()) + 3 * 86400, "trialing"),
    )
    assert applied is True
    assert called["n"] == 0
    # Untouched -- still the ORIGINAL scheduled timestamp, never re-issued. FIX 1a's row
    # lock (`populate_existing=True`) re-reads the row from the DB even when the guard
    # then skips, so the in-memory value is now whatever SQLite round-trips (naive) --
    # normalize before comparing to the aware Python value it was set from (SQLite
    # returns naive; Postgres aware, same idiom as services/onboarding_sync.py).
    stored_cancel_scheduled_at = ent.cancel_scheduled_at
    if stored_cancel_scheduled_at.tzinfo is None:
        stored_cancel_scheduled_at = stored_cancel_scheduled_at.replace(tzinfo=UTC)
    assert stored_cancel_scheduled_at == already_scheduled


async def test_trial_will_end_skipped_when_obj_status_not_trialing(db_session, monkeypatch):
    """The immediate-end firing (harden_charge's own trial_end=now) reports a
    subscription whose `status` is no longer "trialing" -- the CHEAP short-circuit
    skips before even the row lock, let alone the live refetch."""
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan="secretaria_ferro",
        status="trialing",
        stripe_customer_id="cus_twe4",
        stripe_subscription_id="sub_twe4",
    )
    db_session.add(ent)
    await db_session.commit()

    called = {"n": 0}

    async def fake_stripe_post(path, data):
        called["n"] += 1
        return {}

    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_post)
    # NOT mocking _stripe_get -- the cheap short-circuit means it is never called.

    applied = await billing_service.apply_stripe_event(
        db_session,
        "evt_twe_4",
        "customer.subscription.trial_will_end",
        _trial_will_end_obj("sub_twe4", "cus_twe4", int(time.time()), "active"),
    )
    assert applied is True
    assert called["n"] == 0
    assert ent.cancel_scheduled_at is None


async def test_trial_will_end_skipped_when_plan_does_not_enable_secretaria(db_session, monkeypatch):
    """FIX 4: a PreCheck-only (or any non-secretarIA) plan must NEVER be
    auto-cancelled -- it has no WhatsApp/Coexistence component and structurally cannot
    reach 'ativo' (harden_charge only fires from secretarIA onboarding). Fails toward
    charging a possibly-legit subscription, never toward killing one. No Stripe call of
    ANY kind (not even the live GET) -- the plan gate is checked before the refetch."""
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan="precheck",
        status="trialing",
        stripe_customer_id="cus_twe8",
        stripe_subscription_id="sub_twe8",
    )
    db_session.add(ent)
    await db_session.commit()

    called = {"n": 0}

    async def fake_stripe_call(*args, **kwargs):
        called["n"] += 1
        return {}

    monkeypatch.setattr(billing_service, "_stripe_get", fake_stripe_call)
    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_call)

    applied = await billing_service.apply_stripe_event(
        db_session,
        "evt_twe_8",
        "customer.subscription.trial_will_end",
        _trial_will_end_obj("sub_twe8", "cus_twe8", int(time.time()) + 3 * 86400, "trialing"),
    )
    assert applied is True
    assert called["n"] == 0
    assert ent.cancel_scheduled_at is None


async def test_trial_will_end_failing_stripe_post_propagates_and_stays_retriable(
    db_session, monkeypatch
):
    """Deliberately NO fail-soft here (unlike harden_charge): a failure must propagate
    so the caller (the webhook route) 500s and Stripe redelivers -- and the
    ProcessedStripeEvent marker must NOT have committed, since apply_stripe_event
    commits the marker + mutation TOGETHER (a failed apply leaves both uncommitted, so
    a redelivery correctly reprocesses instead of seeing an already-applied duplicate).
    The live refetch (`_stripe_get`) is mocked to SUCCEED here so the failure genuinely
    comes from `_stripe_post`, matching what this test is actually about.
    """
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan="secretaria_ferro",
        status="trialing",
        stripe_customer_id="cus_twe5",
        stripe_subscription_id="sub_twe5",
    )
    db_session.add(ent)
    await db_session.commit()

    async def failing_stripe_post(path, data):
        raise HTTPException(502, "stripe_error")

    monkeypatch.setattr(billing_service, "_stripe_post", failing_stripe_post)
    monkeypatch.setattr(
        billing_service,
        "_stripe_get",
        _live_subscription("trialing", int(time.time()) + 3 * 86400),
    )

    with pytest.raises(HTTPException):
        await billing_service.apply_stripe_event(
            db_session,
            "evt_twe_5",
            "customer.subscription.trial_will_end",
            _trial_will_end_obj("sub_twe5", "cus_twe5", int(time.time()) + 3 * 86400, "trialing"),
        )
    await db_session.rollback()
    # rollback() expires every instance -- refresh explicitly before reading (AsyncSession
    # has no implicit lazy-load; touching an expired attribute without this raises
    # MissingGreenlet, not a stale/None value).
    await db_session.refresh(ent)

    marker = await db_session.get(ProcessedStripeEvent, "evt_twe_5")
    assert marker is None
    assert ent.cancel_scheduled_at is None


# ===========================================================================================
# FIX 2: charge_hardened_at/cancel_scheduled_at must NOT survive a resubscription
# ===========================================================================================


async def test_resubscription_resets_both_markers_and_reprotects_the_new_trial(
    db_session, monkeypatch
):
    """Empirically confirmed bug: without resetting the markers on a subscription-id
    change, a tenant's SECOND subscription would inherit the first one's
    charge_hardened_at/cancel_scheduled_at, permanently disabling both harden_charge and
    trial_will_end scheduling for it -- the new trial would run out completely
    unprotected and the tenant would simply get charged whether or not it ever
    activated. This test hardens a FIRST subscription, cancels it, links a NEW one via
    subscription.created, and proves both markers reset AND the new sub's own
    trial_will_end can schedule a cancellation (proof the reset actually re-enables
    protection, not just that the columns read None)."""
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan="secretaria_ferro",
        status="trialing",
        stripe_customer_id="cus_resub",
        stripe_subscription_id="sub_old",
        charge_hardened_at=datetime.now(UTC),  # the FIRST subscription WAS hardened
    )
    db_session.add(ent)
    await db_session.commit()

    # The old (hardened, paying) subscription gets cancelled -- e.g. a downgrade.
    deleted = await billing_service.apply_stripe_event(
        db_session,
        "evt_resub_deleted",
        "customer.subscription.deleted",
        {"customer": "cus_resub"},
    )
    assert deleted is True
    await db_session.refresh(ent)
    assert ent.status == "canceled"
    # The deleted branch itself does not touch charge_hardened_at -- still set here.
    assert ent.charge_hardened_at is not None

    # A brand-new subscription links via subscription.created with a DIFFERENT id.
    now = int(time.time())
    created = await billing_service.apply_stripe_event(
        db_session,
        "evt_resub_created",
        "customer.subscription.created",
        {
            "id": "sub_new",
            "customer": "cus_resub",
            "status": "trialing",
            "current_period_start": now,
            "current_period_end": now + 86400,
            "items": {"data": [{"price": {"id": "price_ferro"}, "quantity": 1}]},
        },
    )
    assert created is True
    await db_session.refresh(ent)
    assert ent.stripe_subscription_id == "sub_new"
    assert ent.status == "trialing"
    assert ent.plan == "secretaria_basico"
    # BOTH markers reset -- the new subscription starts a fresh trial lifecycle.
    assert ent.charge_hardened_at is None
    assert ent.cancel_scheduled_at is None

    # The new sub's OWN trial_will_end must now be able to schedule a cancellation.
    captured: dict = {}

    async def fake_stripe_post(path, data):
        captured["path"] = path
        captured["data"] = data
        return {}

    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_post)
    monkeypatch.setattr(
        billing_service,
        "_stripe_get",
        _live_subscription("trialing", now + 5 * 86400),
    )

    twe = await billing_service.apply_stripe_event(
        db_session,
        "evt_resub_twe",
        "customer.subscription.trial_will_end",
        _trial_will_end_obj("sub_new", "cus_resub", now + 3 * 86400, "trialing"),
    )
    assert twe is True
    assert captured["path"] == "/v1/subscriptions/sub_new"
    await db_session.refresh(ent)
    assert ent.cancel_scheduled_at is not None


async def test_same_subscription_id_redelivery_does_not_reset_markers(db_session):
    """A redelivered event for the SAME (unchanged) subscription id must NOT reset
    charge_hardened_at -- only a genuine subscription CHANGE triggers the reset."""
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(
        tenant_id=tenant.id,
        plan="secretaria_ferro",
        status="active",
        stripe_customer_id="cus_samesub",
        stripe_subscription_id="sub_same",
        charge_hardened_at=datetime.now(UTC),
    )
    db_session.add(ent)
    await db_session.commit()

    now = int(time.time())
    updated = await billing_service.apply_stripe_event(
        db_session,
        "evt_samesub_updated",
        "customer.subscription.updated",
        {
            "id": "sub_same",
            "customer": "cus_samesub",
            "status": "active",
            "current_period_start": now,
            "current_period_end": now + 86400,
            "items": {"data": [{"price": {"id": "price_ferro"}, "quantity": 1}]},
        },
    )
    assert updated is True
    await db_session.refresh(ent)
    assert ent.stripe_subscription_id == "sub_same"
    assert ent.charge_hardened_at is not None


# ===========================================================================================
# Meter forward (services/usage.py)
# ===========================================================================================


async def _link_stripe_customer(client, tenant_id: str, customer_id: str) -> None:
    obj = {"customer": customer_id, "metadata": {"tenant_id": tenant_id}}
    resp = await _post_webhook(
        client, _event(f"evt_link_{customer_id}", "checkout.session.completed", obj)
    )
    assert resp.status_code == 200, resp.text


async def test_meter_forward_fires_when_configured_and_customer_present(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    await _link_stripe_customer(client, tenant_a_id, "cus_meter_1")

    captured: dict = {}

    async def fake_stripe_post(path, data):
        captured["path"] = path
        captured["data"] = data
        return {}

    monkeypatch.setattr(usage_module.billing, "_stripe_post", fake_stripe_post)
    monkeypatch.setattr(
        usage_module,
        "get_settings",
        lambda: SimpleNamespace(STRIPE_METER_EVENT_BILLABLE_PATIENTS="brainco_billable_patients"),
    )

    resp = await client.post(
        "/internal/usage-events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={
            "tenant_id": tenant_a_id,
            "feature": catalog.LIMIT_BILLABLE_PATIENTS,
            "amount": 1,
            "event_id": "bp:evt-1",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"recorded": True}
    assert captured["path"] == "/v1/billing/meter_events"
    assert captured["data"] == {
        "event_name": "brainco_billable_patients",
        "payload[stripe_customer_id]": "cus_meter_1",
        "payload[value]": "1",
    }


async def test_meter_forward_not_fired_when_event_name_unset(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    await _link_stripe_customer(client, tenant_a_id, "cus_meter_2")

    called = {"n": 0}

    async def fake_stripe_post(path, data):
        called["n"] += 1
        return {}

    monkeypatch.setattr(usage_module.billing, "_stripe_post", fake_stripe_post)
    monkeypatch.setattr(
        usage_module,
        "get_settings",
        lambda: SimpleNamespace(STRIPE_METER_EVENT_BILLABLE_PATIENTS=None),
    )

    resp = await client.post(
        "/internal/usage-events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={
            "tenant_id": tenant_a_id,
            "feature": catalog.LIMIT_BILLABLE_PATIENTS,
            "amount": 2,
            "event_id": "bp:evt-2",
        },
    )
    assert resp.status_code == 200, resp.text
    assert called["n"] == 0


async def test_meter_forward_not_fired_without_stripe_customer_id(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = (await _tenant_ids(client, admin_token))[CLINIC_B]

    called = {"n": 0}

    async def fake_stripe_post(path, data):
        called["n"] += 1
        return {}

    monkeypatch.setattr(usage_module.billing, "_stripe_post", fake_stripe_post)
    monkeypatch.setattr(
        usage_module,
        "get_settings",
        lambda: SimpleNamespace(STRIPE_METER_EVENT_BILLABLE_PATIENTS="brainco_billable_patients"),
    )

    resp = await client.post(
        "/internal/usage-events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={
            "tenant_id": tenant_b_id,
            "feature": catalog.LIMIT_BILLABLE_PATIENTS,
            "amount": 1,
            "event_id": "bp:evt-3",
        },
    )
    assert resp.status_code == 200, resp.text
    assert called["n"] == 0


async def test_meter_forward_not_fired_for_non_billable_patients_feature(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    await _link_stripe_customer(client, tenant_a_id, "cus_meter_4")

    called = {"n": 0}

    async def fake_stripe_post(path, data):
        called["n"] += 1
        return {}

    monkeypatch.setattr(usage_module.billing, "_stripe_post", fake_stripe_post)
    monkeypatch.setattr(
        usage_module,
        "get_settings",
        lambda: SimpleNamespace(STRIPE_METER_EVENT_BILLABLE_PATIENTS="brainco_billable_patients"),
    )

    resp = await client.post(
        "/internal/usage-events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={
            "tenant_id": tenant_a_id,
            "feature": catalog.LIMIT_MESSAGES,
            "amount": 1,
            "event_id": "msg:evt-1",
        },
    )
    assert resp.status_code == 200, resp.text
    assert called["n"] == 0


# ===========================================================================================
# Meter forward: active_professionals (fully-metered secretaria_basico model)
# ===========================================================================================


async def test_meter_forward_fires_for_active_professionals_with_configured_event_name(
    client, monkeypatch
):
    """Same forwarding mechanics as billable_patients, dispatched to the DIFFERENT
    configured event_name for this feature (services.usage._METER_EVENT_SETTINGS)."""
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    await _link_stripe_customer(client, tenant_a_id, "cus_meter_ap_1")

    captured: dict = {}

    async def fake_stripe_post(path, data):
        captured["path"] = path
        captured["data"] = data
        return {}

    monkeypatch.setattr(usage_module.billing, "_stripe_post", fake_stripe_post)
    monkeypatch.setattr(
        usage_module,
        "get_settings",
        lambda: SimpleNamespace(
            STRIPE_METER_EVENT_ACTIVE_PROFESSIONALS="brainco_active_professionals"
        ),
    )

    resp = await client.post(
        "/internal/usage-events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={
            "tenant_id": tenant_a_id,
            "feature": catalog.LIMIT_ACTIVE_PROFESSIONALS,
            "amount": 2,
            "event_id": "ap:evt-1",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"recorded": True}
    assert captured["path"] == "/v1/billing/meter_events"
    assert captured["data"] == {
        "event_name": "brainco_active_professionals",
        "payload[stripe_customer_id]": "cus_meter_ap_1",
        "payload[value]": "2",
    }


async def test_meter_forward_not_fired_for_active_professionals_when_event_name_unset(
    client, monkeypatch
):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    await _link_stripe_customer(client, tenant_a_id, "cus_meter_ap_2")

    called = {"n": 0}

    async def fake_stripe_post(path, data):
        called["n"] += 1
        return {}

    monkeypatch.setattr(usage_module.billing, "_stripe_post", fake_stripe_post)
    monkeypatch.setattr(
        usage_module,
        "get_settings",
        lambda: SimpleNamespace(STRIPE_METER_EVENT_ACTIVE_PROFESSIONALS=None),
    )

    resp = await client.post(
        "/internal/usage-events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={
            "tenant_id": tenant_a_id,
            "feature": catalog.LIMIT_ACTIVE_PROFESSIONALS,
            "amount": 1,
            "event_id": "ap:evt-2",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"recorded": True}
    assert called["n"] == 0


# ===========================================================================================
# Meter forward: reminders (fully-metered secretaria_basico model, third leg)
# ===========================================================================================


async def test_meter_forward_fires_for_reminders_with_configured_event_name(client, monkeypatch):
    """Same forwarding mechanics as billable_patients/active_professionals, dispatched to
    the DIFFERENT configured event_name for this feature
    (services.usage._METER_EVENT_SETTINGS)."""
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    await _link_stripe_customer(client, tenant_a_id, "cus_meter_rem_1")

    captured: dict = {}

    async def fake_stripe_post(path, data):
        captured["path"] = path
        captured["data"] = data
        return {}

    monkeypatch.setattr(usage_module.billing, "_stripe_post", fake_stripe_post)
    monkeypatch.setattr(
        usage_module,
        "get_settings",
        lambda: SimpleNamespace(STRIPE_METER_EVENT_REMINDERS="brainco_reminders"),
    )

    resp = await client.post(
        "/internal/usage-events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={
            "tenant_id": tenant_a_id,
            "feature": catalog.LIMIT_REMINDERS,
            "amount": 4,
            "event_id": "rem:evt-1",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"recorded": True}
    assert captured["path"] == "/v1/billing/meter_events"
    assert captured["data"] == {
        "event_name": "brainco_reminders",
        "payload[stripe_customer_id]": "cus_meter_rem_1",
        "payload[value]": "4",
    }


async def test_meter_forward_not_fired_for_reminders_when_event_name_unset(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    await _link_stripe_customer(client, tenant_a_id, "cus_meter_rem_2")

    called = {"n": 0}

    async def fake_stripe_post(path, data):
        called["n"] += 1
        return {}

    monkeypatch.setattr(usage_module.billing, "_stripe_post", fake_stripe_post)
    monkeypatch.setattr(
        usage_module,
        "get_settings",
        lambda: SimpleNamespace(STRIPE_METER_EVENT_REMINDERS=None),
    )

    resp = await client.post(
        "/internal/usage-events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={
            "tenant_id": tenant_a_id,
            "feature": catalog.LIMIT_REMINDERS,
            "amount": 1,
            "event_id": "rem:evt-2",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"recorded": True}
    assert called["n"] == 0
