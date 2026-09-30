"""Guard on POST /billing/checkout (TASK B introduced it; TASK C narrowed it — spec C §5.14).

One rule, permanent: a LIVE subscription already linked -> 409 `has_active_subscription`,
BEFORE any Stripe call (a second simultaneous subscription is never right; the clinic adds a
product to the one it has: POST /billing/add-product). TASK B's second rule (secretarIA on
without a subscription) is gone: a courtesy/admin clinic may check out, and the webhook merges
the new subscription's families with its manual products (`apply_subscription_state`).
"""

from datetime import UTC, datetime

import pytest

from brain_api.models import Entitlement, Tenant
from brain_api.services.billing import (
    HAS_ACTIVE_SUBSCRIPTION,
    LIVE_SUBSCRIPTION_STATUSES,
    checkout_block_reason,
)
from tests.billing_fakes import set_entitlement, snapshot
from tests.test_billing import (
    _admin_entitlements,
    _event,
    _install_fake_stripe_httpx,
    _post_webhook,
    _tenant_ids,
)
from tests.test_courtesy_coupon import _sessao
from tests.test_rbac import (
    CLINIC_A,
    CLINIC_B,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    OWNER_B_EMAIL,
    OWNER_B_PASSWORD,
    _bearer,
    _token,
)

FAKE_URL = "https://checkout.stripe.test/session"
EVERY_PLAN = (
    "precheck_start",
    "precheck_basic",
    "precheck_advanced",
    "complete_clinic_combo",
    "secretaria_basico",
)


def _ent(**fields: object) -> Entitlement:
    """A transient Entitlement with explicit values (column defaults only apply on flush)."""
    base: dict[str, object] = {
        "plan": "free",
        "status": "inactive",
        "precheck_enabled": False,
        "secretaria_enabled": False,
        "stripe_customer_id": None,
        "stripe_subscription_id": None,
    }
    base.update(fields)
    return Entitlement(**base)


# --- Pure decision ---------------------------------------------------------------------


def test_tenant_without_entitlement_row_is_allowed():
    assert checkout_block_reason(None) is None


def test_fresh_tenant_is_allowed():
    assert checkout_block_reason(_ent()) is None


@pytest.mark.parametrize("live_status", sorted(LIVE_SUBSCRIPTION_STATUSES))
def test_live_subscription_blocks(live_status):
    ent = _ent(
        plan="precheck_basic",
        status=live_status,
        precheck_enabled=True,
        stripe_customer_id="cus_1",
        stripe_subscription_id="sub_1",
    )
    assert checkout_block_reason(ent) == HAS_ACTIVE_SUBSCRIPTION


@pytest.mark.parametrize("dead_status", ["canceled", "inactive"])
def test_dead_subscription_does_not_block_a_resubscribe(dead_status):
    ent = _ent(status=dead_status, stripe_customer_id="cus_1", stripe_subscription_id="sub_old")
    assert checkout_block_reason(ent) is None


def test_live_status_without_subscription_id_is_not_blocked():
    # Admin-provisioned PreCheck clinic: status active, no Stripe linkage at all.
    assert (
        checkout_block_reason(_ent(plan="precheck_basic", status="active", precheck_enabled=True))
        is None
    )


def test_secretaria_without_a_subscription_is_no_longer_blocked():
    """TASK B's rule 2 is gone: a courtesy/admin secretarIA clinic may open a checkout."""
    ent = _ent(plan="secretaria_basico", status="active", secretaria_enabled=True)
    assert checkout_block_reason(ent) is None


# --- Endpoint: POST /billing/checkout ----------------------------------------------------


async def _checkout(client, email: str, password: str, plan: str):
    token = await _token(client, email, password)
    return await client.post("/billing/checkout", headers=_bearer(token), json={"plan": plan})


@pytest.mark.parametrize("plan", EVERY_PLAN)
async def test_paid_secretaria_clinic_is_refused_before_stripe(client, monkeypatch, plan):
    """Variant `paid` (unchanged from TASK B): a live subscription blocks every plan, nothing is
    written, Stripe is never reached."""
    tenant_b = (await _tenant_ids(client))[CLINIC_B]
    await set_entitlement(
        tenant_b,
        plan="secretaria_basico",
        status="active",
        secretaria_enabled=True,
        precheck_enabled=False,
        stripe_customer_id="cus_sec",
        stripe_subscription_id="sub_sec",
    )
    before = await snapshot(tenant_b)
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})

    resp = await _checkout(client, OWNER_B_EMAIL, OWNER_B_PASSWORD, plan)

    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": HAS_ACTIVE_SUBSCRIPTION}
    assert captured == {}, "Stripe must never be reached"
    assert await snapshot(tenant_b) == before


@pytest.mark.parametrize("plan", EVERY_PLAN)
async def test_courtesy_secretaria_clinic_may_open_a_checkout(client, monkeypatch, plan):
    """Variant `courtesy` (was 409 in TASK B): no subscription behind the row -> a checkout URL."""
    tenant_b = (await _tenant_ids(client))[CLINIC_B]
    await set_entitlement(
        tenant_b,
        plan="secretaria_basico",
        status="active",
        secretaria_enabled=True,
        precheck_enabled=False,
        manual_products=["secretaria"],
    )
    before = await snapshot(tenant_b)
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})

    resp = await _checkout(client, OWNER_B_EMAIL, OWNER_B_PASSWORD, plan)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": FAKE_URL}
    assert captured["path"] == "/v1/checkout/sessions"
    assert await snapshot(tenant_b) == before  # opening a checkout writes nothing


async def test_courtesy_secretaria_survives_buying_precheck_through_every_webhook(
    client, monkeypatch
):
    """MANDATORY regression (spec C §5.14): checkout -> completed -> created -> updated (renewal)
    -> invoice.paid never switch the courtesy secretarIA off; deleting the paid PreCheck
    subscription leaves it on and puts the clinic back in the "no subscription" state."""
    tenant_b = (await _tenant_ids(client))[CLINIC_B]
    await set_entitlement(
        tenant_b,
        plan="secretaria_basico",
        status="active",
        secretaria_enabled=True,
        precheck_enabled=False,
        manual_products=["secretaria"],
    )
    _install_fake_stripe_httpx(monkeypatch, {}, {"url": FAKE_URL})
    assert (
        await _checkout(client, OWNER_B_EMAIL, OWNER_B_PASSWORD, "precheck_basic")
    ).status_code == 200

    now = int(datetime.now(UTC).timestamp())
    sub = {
        "id": "sub_c",
        "customer": "cus_c",
        "status": "active",
        "current_period_start": now,
        "current_period_end": now + 30 * 86400,
        "items": {"data": [{"price": {"id": "price_precheck"}, "quantity": 1}]},
        "metadata": {"tenant_id": tenant_b},
    }
    deliveries = [
        (
            "evt_cg1",
            "checkout.session.completed",
            {"customer": "cus_c", "subscription": "sub_c", "metadata": {"tenant_id": tenant_b}},
        ),
        ("evt_cg2", "customer.subscription.created", sub),
        ("evt_cg3", "customer.subscription.updated", sub),
        ("evt_cg4", "invoice.paid", {"customer": "cus_c"}),
    ]
    for event_id, event_type, obj in deliveries:
        resp = await _post_webhook(client, _event(event_id, event_type, obj))
        assert resp.status_code == 200, resp.text
        ent = await _admin_entitlements(client, tenant_b)
        assert ent["secretaria_enabled"] is True, event_type
        assert ent["status"] == "active", event_type
    assert ent["precheck_enabled"] is True
    assert ent["plan"] == "secretaria_basico"
    assert ent["precheck_plan"] == "precheck_basic"
    assert ent["manual_products"] == ["secretaria"]

    resp = await _post_webhook(
        client, _event("evt_cg5", "customer.subscription.deleted", {**sub, "status": "canceled"})
    )
    assert resp.status_code == 200
    ent = await _admin_entitlements(client, tenant_b)
    assert ent["secretaria_enabled"] is True and ent["precheck_enabled"] is False
    assert ent["stripe_subscription_id"] is None and ent["status"] == "active"


@pytest.mark.parametrize("live_status", ["active", "trialing", "past_due"])
async def test_live_subscription_refuses_a_second_checkout(client, monkeypatch, live_status):
    tenant_a = (await _tenant_ids(client))[CLINIC_A]
    await set_entitlement(
        tenant_a, status=live_status, stripe_customer_id="cus_a", stripe_subscription_id="sub_a"
    )
    before = await snapshot(tenant_a)
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})

    resp = await _checkout(client, OWNER_A_EMAIL, OWNER_A_PASSWORD, "precheck_advanced")

    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": HAS_ACTIVE_SUBSCRIPTION}
    assert captured == {}
    assert await snapshot(tenant_a) == before


async def test_clinic_without_any_subscription_still_gets_a_checkout_url(client, monkeypatch):
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})
    resp = await _checkout(client, OWNER_B_EMAIL, OWNER_B_PASSWORD, "precheck_basic")
    assert resp.status_code == 200, resp.text
    assert captured["data"]["line_items[0][price]"] == "price_precheck"


async def test_canceled_subscription_may_check_out_again(client, monkeypatch):
    tenant_a = (await _tenant_ids(client))[CLINIC_A]
    await set_entitlement(
        tenant_a,
        status="canceled",
        precheck_enabled=False,
        secretaria_enabled=False,
        stripe_customer_id="cus_a",
        stripe_subscription_id="sub_old",
    )
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})
    resp = await _checkout(client, OWNER_A_EMAIL, OWNER_A_PASSWORD, "precheck_basic")
    assert resp.status_code == 200, resp.text
    assert captured["data"]["customer"] == "cus_a"


async def test_test_clinic_is_still_refused_by_the_403_door_not_the_guard(client, monkeypatch):
    from uuid import UUID

    tenant_b = (await _tenant_ids(client))[CLINIC_B]
    await set_entitlement(
        tenant_b,
        plan="secretaria_basico",
        status="active",
        secretaria_enabled=True,
        stripe_customer_id="cus_t",
        stripe_subscription_id="sub_t",
    )
    async with _sessao() as session:
        tenant = await session.get(Tenant, UUID(tenant_b))
        tenant.is_test = True
        await session.commit()
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})

    resp = await _checkout(client, OWNER_B_EMAIL, OWNER_B_PASSWORD, "precheck_basic")

    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "test_tenant_billing_disabled"}
    assert captured == {}
