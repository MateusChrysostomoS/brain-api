"""add_product_to_subscription end to end against a fake Stripe (TASK C, spec §5.5 / §5.8)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from brain_api.models import Tenant
from brain_api.services import billing as billing_service, catalog, onboarding_sync
from brain_api.services.billing import (
    AddProductRequest,
    StripeApiError,
    add_product_to_subscription,
)
from tests.billing_fakes import (
    SEC_PRICES,
    FakeStripe,
    install_fake_settings,
    make_clinic,
    make_invoice,
    make_sub,
)

KEY = str(uuid4())
OLD = datetime.now(UTC) - timedelta(days=20)

PREVIEW = make_invoice(proration=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    install_fake_settings(monkeypatch)
    bridges: list[UUID] = []

    async def fake_bridges(session, tenant_id):
        bridges.append(tenant_id)

    monkeypatch.setattr(onboarding_sync, "ensure_products_provisioned", fake_bridges)
    return bridges


@pytest.fixture
def bridges(_env):
    return _env


def _req(product, plan=None, *, confirm=True, key=KEY, addons=()):
    return AddProductRequest(
        product=product, plan=plan, addons=tuple(addons), confirm=confirm, idempotency_key=key
    )


async def _window(db_session, ent, started=OLD, notified=None):
    tenant = await db_session.get(Tenant, ent.tenant_id)
    tenant.test_window_started_at = started
    tenant.test_window_notified_at = notified
    await db_session.commit()


async def _tenant(db_session, ent) -> Tenant:
    tenant = await db_session.get(Tenant, ent.tenant_id, populate_existing=True)
    return tenant


def _aware(value):
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _state(ent) -> dict:
    return {
        "plan": ent.plan,
        "precheck_plan": ent.precheck_plan,
        "precheck": ent.precheck_enabled,
        "secretaria": ent.secretaria_enabled,
        "status": ent.status,
        "manual": list(ent.manual_products),
        "limits": dict(ent.limits),
        "hardened": ent.charge_hardened_at,
    }


# --- confirm=false: preview ---------------------------------------------------------------


async def test_preview_reads_stripe_and_writes_nothing(db_session, monkeypatch, bridges):
    stripe = FakeStripe(live=make_sub("price_pc_basic"), preview=PREVIEW).install(monkeypatch)
    ent = await make_clinic(db_session, None, "precheck_basic")
    await _window(db_session, ent)
    before = _state(ent)

    result = await add_product_to_subscription(
        db_session, ent.tenant_id, _req("secretaria", confirm=False, key=None)
    )

    assert result.status == "preview"
    assert result.charge.amount_due_now_cents == 0
    assert result.charge.next_invoice_cents is None
    assert result.charge.next_invoice_date is None
    assert stripe.kinds() == ["GET", "PREVIEW"]  # never an update
    _, path, form, _ = stripe.call("PREVIEW")
    assert path == "/v1/invoices/create_preview"
    assert form["customer"] == "cus_1" and form["subscription"] == "sub_1"
    assert form["subscription_details[proration_behavior]"] == "none"  # metered-only
    assert [form[f"subscription_details[items][{i}][price]"] for i in range(3)] == list(SEC_PRICES)
    await db_session.refresh(ent)
    assert _state(ent) == before
    assert _aware((await _tenant(db_session, ent)).test_window_started_at) == OLD
    assert bridges == []


async def test_a_confirm_without_the_idempotency_key_is_refused_before_stripe(
    db_session, monkeypatch
):
    stripe = FakeStripe(live=make_sub("price_pc_basic")).install(monkeypatch)
    ent = await make_clinic(db_session, None, "precheck_basic")
    for key, detail in (
        (None, "idempotency_key_required"),
        ("", "idempotency_key_required"),
        ("not-a-uuid", "invalid_idempotency_key"),
    ):
        with pytest.raises(HTTPException) as exc:
            await add_product_to_subscription(
                db_session, ent.tenant_id, _req("secretaria", key=key)
            )
        assert (exc.value.status_code, exc.value.detail) == (422, detail)
    assert stripe.calls == []


# --- confirm=true: secretarIA added to a PreCheck subscription ----------------------------------


async def test_adding_secretaria_to_an_active_precheck_subscription(
    db_session, monkeypatch, bridges
):
    updated = make_sub(
        "price_pc_basic", *SEC_PRICES, latest_invoice={"amount_paid": 0, "currency": "brl"}
    )
    stripe = FakeStripe(live=make_sub("price_pc_basic"), updated=updated).install(monkeypatch)
    ent = await make_clinic(db_session, None, "precheck_basic")
    await _window(db_session, ent, notified=OLD)

    result = await add_product_to_subscription(db_session, ent.tenant_id, _req("secretaria"))

    assert result.status == "added"
    assert result.charge.amount_due_now_cents == 0
    assert stripe.kinds() == ["GET", "PREVIEW", "UPDATE"]  # ordered: read, then write
    _, path, data, idem = stripe.call("UPDATE")
    assert path == "/v1/subscriptions/sub_1"
    assert idem == f"add-product:{ent.tenant_id}:{KEY}"
    assert [data[f"items[{i}][price]"] for i in range(3)] == list(SEC_PRICES)
    assert not any(k.endswith("[quantity]") for k in data)  # Stripe rejects quantity on metered
    assert data["proration_behavior"] == "none"
    assert data["payment_behavior"] == "error_if_incomplete"
    assert data["expand[]"] == "latest_invoice"
    forbidden = [
        k
        for k in data
        if k.startswith(("trial", "subscription_data", "billing_cycle_anchor", "items[0][id]"))
    ]
    assert forbidden == []  # an `active` subscription must never be turned into a trial

    await db_session.refresh(ent)
    assert _state(ent)["plan"] == "secretaria_basico"
    assert ent.precheck_plan == "precheck_basic"
    assert ent.precheck_enabled and ent.secretaria_enabled
    assert ent.status == "active"
    assert ent.charge_hardened_at is not None  # no trial to protect or end
    tenant = await _tenant(db_session, ent)
    assert _aware(tenant.test_window_started_at) > OLD + timedelta(days=19)  # fresh window
    assert tenant.test_window_notified_at is None
    assert bridges == [ent.tenant_id]


# --- confirm=true: PreCheck added to a secretarIA subscription -----------------------------------


async def test_adding_precheck_to_an_active_secretaria_subscription(
    db_session, monkeypatch, bridges
):
    updated = make_sub(
        *SEC_PRICES, "price_pc_adv", latest_invoice={"amount_paid": 12345, "currency": "brl"}
    )
    stripe = FakeStripe(live=make_sub(*SEC_PRICES), updated=updated).install(monkeypatch)
    ent = await make_clinic(db_session, "secretaria_basico", None)
    await _window(db_session, ent)

    result = await add_product_to_subscription(
        db_session, ent.tenant_id, _req("precheck", "precheck_advanced")
    )

    assert result.status == "added"
    assert result.charge.amount_due_now_cents == 12345
    _, _, data, _ = stripe.call("UPDATE")
    assert data["items[0][price]"] == "price_pc_adv" and data["items[0][quantity]"] == "1"
    assert data["proration_behavior"] == "always_invoice"
    await db_session.refresh(ent)
    assert ent.plan == "secretaria_basico"  # the anchor never moves to PreCheck
    assert ent.precheck_plan == "precheck_advanced"
    assert (
        ent.limits[catalog.LIMIT_PRECHECK_CONSULTATIONS]
        == catalog.get_plan("precheck_advanced").base_limits[catalog.LIMIT_PRECHECK_CONSULTATIONS]
    )
    assert ent.limits[catalog.LIMIT_MESSAGES] == 400  # secretarIA limits untouched
    assert ent.charge_hardened_at is None  # only stamped when secretarIA is what was added
    assert (
        _aware((await _tenant(db_session, ent)).test_window_started_at) == OLD
    )  # PreCheck never restarts it
    assert bridges == [ent.tenant_id]


# --- failures leave NOTHING behind ---------------------------------------------------------------


async def test_card_declined_writes_nothing_locally(db_session, monkeypatch, bridges):
    error = StripeApiError(402, "card_declined", "card_error", "Your card was declined.")
    stripe = FakeStripe(live=make_sub("price_pc_basic"), post_error=error).install(monkeypatch)
    ent = await make_clinic(db_session, None, "precheck_basic")
    await _window(db_session, ent, notified=OLD)
    before = _state(ent)

    with pytest.raises(HTTPException) as exc:
        await add_product_to_subscription(db_session, ent.tenant_id, _req("secretaria"))

    assert (exc.value.status_code, exc.value.detail) == (402, "payment_failed")
    assert stripe.kinds() == ["GET", "PREVIEW", "UPDATE"]
    await db_session.refresh(ent)
    assert _state(ent) == before
    tenant = await _tenant(db_session, ent)
    assert (
        _aware(tenant.test_window_started_at) == OLD and tenant.test_window_notified_at is not None
    )
    assert bridges == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            StripeApiError(402, "authentication_required", "card_error", None),
            (409, "payment_action_required"),
        ),
        (
            StripeApiError(
                400,
                None,
                "invalid_request_error",
                "This customer has no attached payment source or default payment method.",
            ),
            (409, "payment_method_required"),
        ),
        (StripeApiError(500, None, "api_error", None), (502, "stripe_error")),
    ],
)
async def test_other_stripe_refusals_are_translated(db_session, monkeypatch, error, expected):
    FakeStripe(live=make_sub("price_pc_basic"), post_error=error).install(monkeypatch)
    ent = await make_clinic(db_session, None, "precheck_basic")
    with pytest.raises(HTTPException) as exc:
        await add_product_to_subscription(db_session, ent.tenant_id, _req("secretaria"))
    assert (exc.value.status_code, exc.value.detail) == expected


async def test_a_response_without_the_requested_family_is_not_reported_as_added(
    db_session, monkeypatch, bridges
):
    FakeStripe(live=make_sub("price_pc_basic"), updated=make_sub("price_pc_basic")).install(
        monkeypatch
    )
    ent = await make_clinic(db_session, None, "precheck_basic")
    before = _state(ent)
    with pytest.raises(HTTPException) as exc:
        await add_product_to_subscription(db_session, ent.tenant_id, _req("secretaria"))
    assert (exc.value.status_code, exc.value.detail) == (502, "add_product_unconfirmed")
    await db_session.refresh(ent)
    assert _state(ent) == before
    assert bridges == []


# --- idempotency / races -------------------------------------------------------------------------


async def test_already_present_recomposes_the_row_without_updating_stripe(
    db_session, monkeypatch, bridges
):
    stripe = FakeStripe(live=make_sub("price_pc_basic", *SEC_PRICES)).install(monkeypatch)
    ent = await make_clinic(db_session, None, "precheck_basic")  # the webhook has not arrived yet

    result = await add_product_to_subscription(db_session, ent.tenant_id, _req("secretaria"))

    assert result.status == "already_present"
    assert stripe.kinds() == ["GET"]
    await db_session.refresh(ent)
    assert ent.precheck_enabled and ent.secretaria_enabled
    assert ent.precheck_plan == "precheck_basic"
    assert bridges == [ent.tenant_id]


async def test_endpoint_and_webhook_converge_in_either_order(db_session, monkeypatch, bridges):
    """The webhook `updated` and the endpoint derive from the SAME subscription object with the
    SAME function: whichever lands first, the row is identical and the window restarts ONCE."""
    live = make_sub("price_pc_basic")
    after = make_sub(
        "price_pc_basic", *SEC_PRICES, latest_invoice={"amount_paid": 0, "currency": "brl"}
    )

    # Order 1: endpoint first, then the webhook event for the same subscription.
    a = await make_clinic(db_session, None, "precheck_basic")
    await _window(db_session, a)
    FakeStripe(live=live, updated=after).install(monkeypatch)
    await add_product_to_subscription(db_session, a.tenant_id, _req("secretaria"))
    started_after_endpoint = _aware((await _tenant(db_session, a)).test_window_started_at)
    await billing_service.apply_stripe_event(
        db_session,
        "evt_conv_1",
        "customer.subscription.updated",
        {**after, "metadata": {"tenant_id": str(a.tenant_id)}},
    )
    await db_session.refresh(a)
    assert _aware((await _tenant(db_session, a)).test_window_started_at) == started_after_endpoint

    # Order 2: the webhook lands while the endpoint's Stripe call is in flight.
    b = await make_clinic(db_session, None, "precheck_basic")
    await _window(db_session, b)
    seen: dict = {}

    async def webhook_first():
        await billing_service.apply_stripe_event(
            db_session,
            "evt_conv_2",
            "customer.subscription.updated",
            {**after, "metadata": {"tenant_id": str(b.tenant_id)}},
        )
        seen["started"] = _aware((await _tenant(db_session, b)).test_window_started_at)

    FakeStripe(live=live, updated=after, on_update=webhook_first).install(monkeypatch)
    await add_product_to_subscription(db_session, b.tenant_id, _req("secretaria"))
    await db_session.refresh(b)
    assert (
        _aware((await _tenant(db_session, b)).test_window_started_at) == seen["started"]
    )  # no 2nd restart

    view_a, view_b = _state(a), _state(b)
    for key in ("plan", "precheck_plan", "precheck", "secretaria", "status", "manual", "limits"):
        assert view_a[key] == view_b[key], key
