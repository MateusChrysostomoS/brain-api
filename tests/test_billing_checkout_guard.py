"""TASK B guard on POST /billing/checkout (removed by TASK C, spec 2026-09-29 §5.3).

One Stripe subscription -> one `plan` (+ add-ons) in the entitlement
(`services.billing._state_from_subscription`). A second checkout for a tenant that
already has a live subscription, or that already runs secretarIA, would make the
webhook recompute the entitlement from the NEW subscription alone: secretaria_enabled
switched off, stripe_subscription_id replaced, test window restarted. The guard
refuses such a checkout with 409 `has_active_subscription` BEFORE any Stripe call.
"""

from uuid import UUID

import pytest

from brain_api.models import Entitlement, Tenant
from brain_api.services import billing as billing_service
from brain_api.services.billing import (
    HAS_ACTIVE_SUBSCRIPTION,
    CheckoutSelection,
    checkout_block_reason,
)
from tests.test_billing import _install_fake_stripe_httpx, _tenant_ids
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

PRECHECK_BASIC = CheckoutSelection(plan_id="precheck_basic", addon_ids=())
PRECHECK_ADVANCED = CheckoutSelection(plan_id="precheck_advanced", addon_ids=())
SECRETARIA = CheckoutSelection(plan_id="secretaria_basico", addon_ids=())
COMBO = CheckoutSelection(plan_id="complete_clinic_combo", addon_ids=())
EVERY_PLAN = (PRECHECK_BASIC, PRECHECK_ADVANCED, SECRETARIA, COMBO)


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


def test_live_statuses_are_values_stripe_webhook_can_write():
    assert billing_service.LIVE_SUBSCRIPTION_STATUSES == {"active", "trialing", "past_due"}
    assert billing_service.LIVE_SUBSCRIPTION_STATUSES <= set(billing_service._STATUS_MAP.values())


def test_tenant_without_entitlement_row_is_allowed():
    assert checkout_block_reason(None, PRECHECK_BASIC) is None


def test_fresh_tenant_is_allowed_for_every_plan():
    for selection in EVERY_PLAN:
        assert checkout_block_reason(_ent(), selection) is None


@pytest.mark.parametrize("live_status", ["active", "trialing", "past_due"])
def test_live_subscription_blocks_every_plan(live_status):
    ent = _ent(
        plan="precheck_basic",
        status=live_status,
        precheck_enabled=True,
        stripe_customer_id="cus_1",
        stripe_subscription_id="sub_1",
    )
    for selection in EVERY_PLAN:
        assert checkout_block_reason(ent, selection) == HAS_ACTIVE_SUBSCRIPTION


@pytest.mark.parametrize("dead_status", ["canceled", "inactive"])
def test_dead_subscription_does_not_block_a_resubscribe(dead_status):
    ent = _ent(status=dead_status, stripe_customer_id="cus_1", stripe_subscription_id="sub_old")
    assert checkout_block_reason(ent, PRECHECK_BASIC) is None


def test_live_status_without_subscription_id_is_not_the_first_rule():
    # Admin-provisioned PreCheck clinic: status active, no Stripe linkage at all.
    ent = _ent(plan="precheck_basic", status="active", precheck_enabled=True)
    assert checkout_block_reason(ent, SECRETARIA) is None


def test_secretaria_without_subscription_blocks_every_plan():
    # Courtesy coupon / admin PATCH / POST /admin/tenants: secretarIA on, no subscription.
    ent = _ent(plan="secretaria_basico", status="active", secretaria_enabled=True)
    for selection in EVERY_PLAN:
        assert checkout_block_reason(ent, selection) == HAS_ACTIVE_SUBSCRIPTION


def test_secretaria_flag_only_matters_for_a_plan_with_a_product():
    ent = _ent(plan="secretaria_basico", status="active", secretaria_enabled=True)
    assert checkout_block_reason(ent, CheckoutSelection(plan_id="free", addon_ids=())) is None


# --- Endpoint: POST /billing/checkout --------------------------------------------------

_SNAPSHOT_FIELDS = (
    "plan",
    "status",
    "precheck_enabled",
    "secretaria_enabled",
    "addons",
    "limits",
    "stripe_customer_id",
    "stripe_subscription_id",
    "period_start",
    "period_end",
    "charge_hardened_at",
    "cancel_scheduled_at",
)

FAKE_URL = "https://checkout.stripe.test/session"


async def _set_entitlement(tenant_id: str, **fields: object) -> None:
    """Write entitlement columns straight to the DB the `client` app uses (no bridges,
    no webhook) — the state a courtesy/admin/paid clinic is in before it clicks buy."""
    async with _sessao() as session:
        tid = UUID(tenant_id)
        ent = await session.get(Entitlement, tid)
        if ent is None:
            ent = Entitlement(tenant_id=tid)
            session.add(ent)
        for name, value in fields.items():
            setattr(ent, name, value)
        await session.commit()


async def _snapshot(tenant_id: str) -> dict:
    """Every billing-relevant column + the tenant's test window (the three things a
    second subscription would silently rewrite)."""
    async with _sessao() as session:
        tid = UUID(tenant_id)
        ent = await session.get(Entitlement, tid)
        tenant = await session.get(Tenant, tid)
        assert ent is not None and tenant is not None
        snap = {name: getattr(ent, name) for name in _SNAPSHOT_FIELDS}
        snap["test_window_started_at"] = tenant.test_window_started_at
        return snap


async def _checkout(client, email: str, password: str, plan: str):
    token = await _token(client, email, password)
    return await client.post("/billing/checkout", headers=_bearer(token), json={"plan": plan})


_SECRETARIA_ONLY = {
    "paid": {"stripe_customer_id": "cus_sec", "stripe_subscription_id": "sub_sec"},
    "courtesy": {"stripe_customer_id": None, "stripe_subscription_id": None},
}


@pytest.mark.parametrize("setup", sorted(_SECRETARIA_ONLY))
@pytest.mark.parametrize(
    "plan",
    [
        "precheck_start",
        "precheck_basic",
        "precheck_advanced",
        "complete_clinic_combo",
        "secretaria_basico",
    ],
)
async def test_secretaria_only_clinic_never_loses_secretaria_through_checkout(
    client, monkeypatch, setup, plan
):
    """MANDATORY regression (spec §5.3): a secretarIA-only clinic — paid or courtesy —
    is refused before Stripe and its entitlement/test window stay byte-identical."""
    tenant_b = (await _tenant_ids(client))[CLINIC_B]
    await _set_entitlement(
        tenant_b,
        plan="secretaria_basico",
        status="active",
        secretaria_enabled=True,
        precheck_enabled=False,
        **_SECRETARIA_ONLY[setup],
    )
    before = await _snapshot(tenant_b)
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})

    resp = await _checkout(client, OWNER_B_EMAIL, OWNER_B_PASSWORD, plan)

    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": HAS_ACTIVE_SUBSCRIPTION}
    assert captured == {}, "Stripe must never be reached"
    after = await _snapshot(tenant_b)
    assert after == before
    assert after["secretaria_enabled"] is True


@pytest.mark.parametrize("live_status", ["active", "trialing", "past_due"])
async def test_live_subscription_refuses_a_second_checkout(client, monkeypatch, live_status):
    tenant_a = (await _tenant_ids(client))[CLINIC_A]
    await _set_entitlement(
        tenant_a,
        status=live_status,
        stripe_customer_id="cus_a",
        stripe_subscription_id="sub_a",
    )
    before = await _snapshot(tenant_a)
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})

    resp = await _checkout(client, OWNER_A_EMAIL, OWNER_A_PASSWORD, "precheck_advanced")

    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": HAS_ACTIVE_SUBSCRIPTION}
    assert captured == {}
    assert await _snapshot(tenant_a) == before


async def test_clinic_without_any_subscription_still_gets_a_checkout_url(client, monkeypatch):
    # CLINIC_B is seeded with NO entitlement row (tests/test_rbac.py).
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})

    resp = await _checkout(client, OWNER_B_EMAIL, OWNER_B_PASSWORD, "precheck_basic")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": FAKE_URL}
    assert captured["path"] == "/v1/checkout/sessions"
    assert captured["data"]["line_items[0][price]"] == "price_precheck"


async def test_canceled_subscription_may_check_out_again(client, monkeypatch):
    tenant_a = (await _tenant_ids(client))[CLINIC_A]
    await _set_entitlement(
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
    tenant_b = (await _tenant_ids(client))[CLINIC_B]
    await _set_entitlement(
        tenant_b, plan="secretaria_basico", status="active", secretaria_enabled=True
    )
    async with _sessao() as session:
        tenant = await session.get(Tenant, UUID(tenant_b))
        assert tenant is not None
        tenant.is_test = True
        await session.commit()
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": FAKE_URL})

    resp = await _checkout(client, OWNER_B_EMAIL, OWNER_B_PASSWORD, "precheck_basic")

    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "test_tenant_billing_disabled"}
    assert captured == {}
