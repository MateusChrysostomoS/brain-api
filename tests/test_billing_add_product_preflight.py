"""Preflight of add_product_to_subscription: every refusal happens BEFORE any write, and the
launch gate (D6) before any Stripe call (TASK C, spec §5.5 steps 1-5)."""

import pytest
from fastapi import HTTPException

from brain_api.config import Settings
from brain_api.services import billing as billing_service
from brain_api.services.billing import AddProductRequest, _add_product_preflight
from tests.billing_fakes import SEC_PRICES, FakeStripe, install_fake_settings, make_clinic, make_sub


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    install_fake_settings(monkeypatch)


def _req(product="secretaria", plan=None, addons=(), confirm=False, key=None) -> AddProductRequest:
    return AddProductRequest(
        product=product, plan=plan, addons=tuple(addons), confirm=confirm, idempotency_key=key
    )


async def _refusal(db_session, ent, req) -> tuple[int, str]:
    with pytest.raises(HTTPException) as exc:
        await _add_product_preflight(db_session, ent.tenant_id, req)
    return exc.value.status_code, exc.value.detail


def test_the_launch_gate_defaults_to_off():
    assert Settings(_env_file=None).BILLING_ADD_SECRETARIA_ENABLED is False


async def test_secretaria_add_is_blocked_before_any_stripe_call_when_not_launched(
    db_session, monkeypatch
):
    install_fake_settings(monkeypatch, BILLING_ADD_SECRETARIA_ENABLED=False)
    stripe = FakeStripe(live=make_sub("price_pc_basic")).install(monkeypatch)
    ent = await make_clinic(db_session, None, "precheck_basic")
    assert await _refusal(db_session, ent, _req("secretaria")) == (403, "product_not_launched")
    assert stripe.calls == []


async def test_the_launch_gate_does_not_touch_adding_precheck(db_session, monkeypatch):
    install_fake_settings(monkeypatch, BILLING_ADD_SECRETARIA_ENABLED=False)
    stripe = FakeStripe(live=make_sub(*SEC_PRICES)).install(monkeypatch)
    ent = await make_clinic(db_session, "secretaria_basico", None)
    pre = await _add_product_preflight(
        db_session, ent.tenant_id, _req("precheck", "precheck_basic")
    )
    assert pre.already_present is False
    assert stripe.kinds() == ["GET"]


async def test_no_subscription_is_409_and_never_reaches_stripe(db_session, monkeypatch):
    stripe = FakeStripe().install(monkeypatch)
    ent = await make_clinic(
        db_session, None, "precheck_basic", subscription=None, manual=["precheck"]
    )
    assert await _refusal(db_session, ent, _req()) == (409, "no_active_subscription")
    assert stripe.calls == []


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        ("past_due", "subscription_past_due"),
        ("trialing", "subscription_trialing"),
        ("canceled", "no_active_subscription"),
        ("inactive", "no_active_subscription"),
    ],
)
async def test_only_an_active_subscription_may_receive_a_product(
    db_session, monkeypatch, status, detail
):
    stripe = FakeStripe().install(monkeypatch)
    ent = await make_clinic(db_session, None, "precheck_basic", status=status)
    assert await _refusal(db_session, ent, _req()) == (409, detail)
    assert stripe.calls == []


async def test_a_product_the_subscription_already_pays_for_is_refused(db_session, monkeypatch):
    stripe = FakeStripe().install(monkeypatch)
    dual = await make_clinic(db_session, "secretaria_basico", "precheck_basic")
    assert await _refusal(db_session, dual, _req("secretaria")) == (409, "product_already_active")
    assert await _refusal(db_session, dual, _req("precheck", "precheck_start")) == (
        409,
        "product_already_active",
    )
    combo = await make_clinic(db_session, "complete_clinic_combo", None)
    assert await _refusal(db_session, combo, _req("secretaria")) == (409, "product_already_active")
    assert await _refusal(db_session, combo, _req("precheck", "precheck_basic")) == (
        409,
        "product_already_active",
    )
    assert stripe.calls == []


async def test_a_manual_product_may_be_bought_and_converted_to_paid(db_session, monkeypatch):
    """Courtesy secretarIA + a paid PreCheck subscription: adding secretarIA to it is allowed
    (D8: manual -> subscription)."""
    stripe = FakeStripe(live=make_sub("price_pc_basic")).install(monkeypatch)
    ent = await make_clinic(
        db_session, "secretaria_basico", "precheck_basic", manual=["secretaria"]
    )
    pre = await _add_product_preflight(db_session, ent.tenant_id, _req("secretaria"))
    assert pre.already_present is False
    assert [price for price, _ in pre.items] == list(SEC_PRICES)
    assert stripe.kinds() == ["GET"]


@pytest.mark.parametrize(
    ("product", "plan"),
    [
        ("secretaria", "precheck_basic"),  # a plan is forbidden for secretarIA
        ("precheck", None),  # required for PreCheck
        ("precheck", "secretaria_basico"),
        ("precheck", "complete_clinic_combo"),  # the combo is never a tier
        ("precheck", "precheck"),  # legacy alias is not accepted for NEW purchases
    ],
)
async def test_the_plan_must_fit_the_product(db_session, monkeypatch, product, plan):
    stripe = FakeStripe().install(monkeypatch)
    ent = await make_clinic(
        db_session,
        "secretaria_basico" if product == "precheck" else None,
        None if product == "precheck" else "precheck_basic",
    )
    assert await _refusal(db_session, ent, _req(product, plan)) == (422, "invalid_plan_for_product")
    assert stripe.calls == []


async def test_unknown_addon_is_422_and_a_missing_price_is_503(db_session, monkeypatch):
    stripe = FakeStripe().install(monkeypatch)
    ent = await make_clinic(db_session, "secretaria_basico", None)
    assert await _refusal(db_session, ent, _req("precheck", "precheck_basic", addons=["nope"])) == (
        422,
        "unknown_addon:nope",
    )
    install_fake_settings(monkeypatch, STRIPE_PRICE_MAP='{"precheck_basic": "price_pc_basic"}')
    assert await _refusal(db_session, ent, _req("precheck", "precheck_start")) == (
        503,
        "price_not_configured:precheck_start",
    )
    assert stripe.calls == []


@pytest.mark.parametrize(
    ("live_fields", "expected"),
    [
        ({"customer": "cus_someone_else"}, (409, "subscription_shape_unsupported")),
        ({"schedule": "sub_sched_1"}, (409, "subscription_shape_unsupported")),
        ({"status": "trialing"}, (409, "subscription_trialing")),
        ({"status": "past_due"}, (409, "subscription_past_due")),
        ({"status": "canceled"}, (409, "no_active_subscription")),
    ],
)
async def test_the_live_subscription_is_checked_against_the_tenant(
    db_session, monkeypatch, live_fields, expected
):
    live = make_sub("price_pc_basic", **{"customer": "cus_1", **live_fields})
    stripe = FakeStripe(live=live).install(monkeypatch)
    ent = await make_clinic(db_session, None, "precheck_basic")
    assert await _refusal(db_session, ent, _req()) == expected
    assert stripe.kinds() == ["GET"]  # the refusal happened AFTER the read, BEFORE any write


async def test_a_family_the_live_subscription_already_carries_is_already_present(
    db_session, monkeypatch
):
    stripe = FakeStripe(live=make_sub("price_pc_basic", *SEC_PRICES)).install(monkeypatch)
    ent = await make_clinic(db_session, None, "precheck_basic")  # local row lags Stripe
    pre = await _add_product_preflight(db_session, ent.tenant_id, _req("secretaria"))
    assert pre.already_present is True
    assert pre.items == []
    assert stripe.kinds() == ["GET"]


async def test_an_addon_already_on_the_subscription_is_refused(db_session, monkeypatch):
    live = make_sub(*SEC_PRICES, "price_multipro")
    FakeStripe(live=live).install(monkeypatch)
    ent = await make_clinic(db_session, "secretaria_basico", None)
    assert await _refusal(
        db_session, ent, _req("precheck", "precheck_basic", addons=["multi_professional"])
    ) == (
        409,
        "addon_already_on_subscription:multi_professional",
    )


async def test_items_are_flat_for_precheck_and_metered_for_secretaria(db_session, monkeypatch):
    FakeStripe(live=make_sub(*SEC_PRICES)).install(monkeypatch)
    sec_clinic = await make_clinic(db_session, "secretaria_basico", None)
    pre = await _add_product_preflight(
        db_session, sec_clinic.tenant_id, _req("precheck", "precheck_advanced")
    )
    assert pre.items == [("price_pc_adv", "1")]

    FakeStripe(live=make_sub("price_pc_basic")).install(monkeypatch)
    pc_clinic = await make_clinic(db_session, None, "precheck_basic")
    pre = await _add_product_preflight(db_session, pc_clinic.tenant_id, _req("secretaria"))
    assert pre.items == [(price, None) for price in SEC_PRICES]
    assert billing_service.proration_for_items(pre.items) == "none"
