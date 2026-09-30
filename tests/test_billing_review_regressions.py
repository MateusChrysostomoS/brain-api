"""Final review regressions: financial consent, independent writers and manual families."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.models import Entitlement
from brain_api.services import billing
from tests.billing_fakes import SEC_PRICES, FakeStripe, install_fake_settings, make_clinic, make_sub


def invoice(amount=4500, **changes):
    body = {
        "currency": "brl",
        "amount_due": amount,
        "subtotal": amount,
        "total": amount,
        "starting_balance": 0,
        "ending_balance": 0,
        "billing_reason": "subscription_update",
        "lines": {"has_more": False, "data": [{"amount": amount, "proration": True}]},
    }
    body.update(changes)
    return body


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    install_fake_settings(monkeypatch, BILLING_ADD_SECRETARIA_ENABLED=True)


@pytest.mark.parametrize(
    "changes",
    [
        {"total_tax_amounts": [{"amount": 450}]},
        {"total_discount_amounts": [{"amount": 50}]},
        {"starting_balance": -500},
        {"amount_due": 4000},
        {"lines": {"has_more": True, "data": [{"amount": 4500, "proration": True}]}},
        {"lines": {"has_more": False, "data": [{"amount": 4500}]}},
        {
            "lines": {
                "has_more": False,
                "data": [{"amount": 4500, "proration": True}, {"amount": 9000, "proration": False}],
            }
        },
    ],
)
def test_complex_or_partial_invoice_fails_closed(changes):
    with pytest.raises(HTTPException) as exc:
        billing.parse_preview_charge(invoice(**changes), make_sub(*SEC_PRICES))
    assert (exc.value.status_code, exc.value.detail) == (502, "preview_unavailable")


def test_supported_invoice_uses_actual_due_and_does_not_invent_renewal():
    charge = billing.parse_preview_charge(invoice(), make_sub(*SEC_PRICES))
    assert charge.amount_due_now_cents == 4500
    assert charge.next_invoice_cents is None and charge.next_invoice_date is None


async def test_confirmation_repreviews_and_pins_server_date(db_session, monkeypatch):
    ent = await make_clinic(db_session, "secretaria_basico", None)
    stripe = FakeStripe(
        live=make_sub(*SEC_PRICES),
        preview=invoice(),
        updated=make_sub(*SEC_PRICES, "price_pc_basic"),
    ).install(monkeypatch)
    shown = await billing.add_product_to_subscription(
        db_session,
        ent.tenant_id,
        billing.AddProductRequest("precheck", "precheck_basic", (), False, None),
    )
    stripe.calls.clear()
    req = billing.AddProductRequest(
        "precheck",
        "precheck_basic",
        (),
        True,
        str(uuid4()),
        expected_charge=shown.charge,
        quote_token=shown.quote_token,
    )
    await billing.add_product_to_subscription(db_session, ent.tenant_id, req)
    assert stripe.kinds() == ["GET", "PREVIEW", "UPDATE"]
    preview_date = stripe.calls[1][2]["subscription_details[proration_date]"]
    assert stripe.calls[2][2]["proration_date"] == preview_date
    assert abs(int(preview_date) - int(datetime.now(UTC).timestamp())) < 10


async def test_changed_expected_charge_refuses_before_update(db_session, monkeypatch):
    ent = await make_clinic(db_session, "secretaria_basico", None)
    stripe = FakeStripe(live=make_sub(*SEC_PRICES), preview=invoice(5000)).install(monkeypatch)
    stripe.preview = invoice(4500)
    shown = await billing.add_product_to_subscription(
        db_session,
        ent.tenant_id,
        billing.AddProductRequest("precheck", "precheck_basic", (), False, None),
    )
    stripe.calls.clear()
    stripe.preview = invoice(5000)
    expected = shown.charge
    req = billing.AddProductRequest(
        "precheck",
        "precheck_basic",
        (),
        True,
        str(uuid4()),
        expected_charge=expected,
        quote_token=shown.quote_token,
    )
    with pytest.raises(HTTPException) as exc:
        await billing.add_product_to_subscription(db_session, ent.tenant_id, req)
    assert (exc.value.status_code, exc.value.detail) == (409, "preview_changed")
    assert stripe.kinds() == ["GET", "PREVIEW"]
    assert not ent.precheck_enabled


@pytest.mark.parametrize("replacement", [False, True])
async def test_endpoint_never_overwrites_independent_cancel_or_replacement(
    db_session, monkeypatch, replacement
):
    ent = await make_clinic(db_session, "secretaria_basico", None)

    async def competing_writer():
        async with AsyncSession(bind=db_session.bind, expire_on_commit=False) as other:
            row = await other.get(Entitlement, ent.tenant_id)
            row.status = "active" if replacement else "canceled"
            row.stripe_subscription_id = "sub_new" if replacement else "sub_1"
            row.precheck_enabled = False
            row.secretaria_enabled = replacement
            await other.commit()

    FakeStripe(
        live=make_sub(*SEC_PRICES),
        preview=invoice(),
        updated=make_sub(*SEC_PRICES, "price_pc_basic"),
        on_update=competing_writer,
    ).install(monkeypatch)
    req = billing.AddProductRequest("precheck", "precheck_basic", (), True, str(uuid4()))
    with pytest.raises(HTTPException) as exc:
        await billing.add_product_to_subscription(db_session, ent.tenant_id, req)
    assert (exc.value.status_code, exc.value.detail) == (409, "subscription_changed")
    await db_session.refresh(ent)
    assert ent.stripe_subscription_id == ("sub_new" if replacement else "sub_1")
    assert ent.status == ("active" if replacement else "canceled")
    assert not ent.precheck_enabled


async def test_webhook_refreshes_a_stale_identity_map_before_applying(db_session):
    ent = await make_clinic(db_session, "secretaria_basico", None)
    async with AsyncSession(bind=db_session.bind, expire_on_commit=False) as other:
        row = await other.get(Entitlement, ent.tenant_id)
        row.stripe_subscription_id = "sub_new"
        await other.commit()
    await billing.apply_stripe_event(
        db_session,
        "evt_stale_map",
        "customer.subscription.updated",
        {**make_sub("price_pc_basic"), "metadata": {"tenant_id": str(ent.tenant_id)}},
    )
    await db_session.refresh(ent)
    assert ent.stripe_subscription_id == "sub_new"
    assert ent.secretaria_enabled and not ent.precheck_enabled


@pytest.mark.parametrize("paid", ["precheck", "secretaria"])
def test_manual_combo_conversion_renewal_and_deletion_grants_only_owned_families(paid):
    ent = Entitlement(
        plan="complete_clinic_combo",
        status="active",
        precheck_enabled=True,
        secretaria_enabled=True,
        manual_products=["precheck", "secretaria"],
        addons={},
        limits={},
        stripe_subscription_id="sub_1",
    )
    sub = make_sub("price_pc_basic") if paid == "precheck" else make_sub(*SEC_PRICES)
    billing.apply_subscription_state(ent, sub)
    billing.apply_subscription_state(ent, sub)
    if paid == "precheck":
        assert ent.plan == "secretaria_basico" and ent.precheck_plan == "precheck_basic"
    billing.apply_subscription_state(ent, {**sub, "status": "canceled"}, deleted=True)
    assert ent.precheck_enabled is (paid == "secretaria")
    assert ent.secretaria_enabled is (paid == "precheck")
    assert ent.stripe_subscription_id is None


async def test_confirmed_retry_reconciles_a_completed_webhook_without_mutation(
    db_session, monkeypatch
):
    ent = await make_clinic(db_session, "secretaria_basico", "precheck_basic")
    stripe = FakeStripe(live=make_sub(*SEC_PRICES, "price_pc_basic")).install(monkeypatch)
    req = billing.AddProductRequest("precheck", "precheck_basic", (), True, str(uuid4()))
    result = await billing.add_product_to_subscription(db_session, ent.tenant_id, req)
    assert result.status == "already_present"
    assert stripe.kinds() == ["GET"]


async def test_quote_is_scoped_and_pins_identical_update_payload_across_retries(
    db_session, monkeypatch
):
    from brain_api.core.security import decode_token

    ent = await make_clinic(db_session, "secretaria_basico", None)
    stripe = FakeStripe(
        live=make_sub(*SEC_PRICES),
        preview=invoice(),
        post_error=billing.StripeApiError(409, "idempotency_key_in_use", None, None),
    ).install(monkeypatch)
    preview = await billing.add_product_to_subscription(
        db_session,
        ent.tenant_id,
        billing.AddProductRequest("precheck", "precheck_basic", (), False, None),
    )
    assert preview.quote_token and decode_token(preview.quote_token) is None
    req = billing.AddProductRequest(
        "precheck",
        "precheck_basic",
        (),
        True,
        str(uuid4()),
        expected_charge=preview.charge,
        quote_token=preview.quote_token,
    )
    for _ in range(2):
        with pytest.raises(HTTPException) as exc:
            await billing.add_product_to_subscription(db_session, ent.tenant_id, req)
        assert exc.value.detail == "add_product_in_progress"
    updates = [call for call in stripe.calls if call[0] == "UPDATE"]
    assert updates[0][2:] == updates[1][2:]


@pytest.mark.parametrize("kind", ["tampered", "expired", "tenant", "selection"])
async def test_bad_quote_never_mutates_stripe(db_session, monkeypatch, kind):
    from jose import jwt

    ent = await make_clinic(db_session, "secretaria_basico", None)
    stripe = FakeStripe(live=make_sub(*SEC_PRICES), preview=invoice()).install(monkeypatch)
    result = await billing.add_product_to_subscription(
        db_session,
        ent.tenant_id,
        billing.AddProductRequest("precheck", "precheck_basic", (), False, None),
    )
    token = result.quote_token
    if kind == "tampered":
        token = token[:-3] + "xyz"
    else:
        claims = jwt.get_unverified_claims(token)
        claims[{"expired": "exp", "tenant": "tenant", "selection": "plan"}[kind]] = {
            "expired": 1,
            "tenant": str(uuid4()),
            "selection": "precheck_advanced",
        }[kind]
        token = jwt.encode(claims, billing._quote_signing_key(), algorithm="HS256")
    req = billing.AddProductRequest(
        "precheck",
        "precheck_basic",
        (),
        True,
        str(uuid4()),
        quote_token=token,
        expected_charge=result.charge,
    )
    with pytest.raises(HTTPException) as exc:
        await billing.add_product_to_subscription(db_session, ent.tenant_id, req)
    assert exc.value.status_code in (409, 422)
    assert "UPDATE" not in stripe.kinds()


async def test_expected_charge_requires_a_server_quote(db_session, monkeypatch):
    ent = await make_clinic(db_session, "secretaria_basico", None)
    stripe = FakeStripe(live=make_sub(*SEC_PRICES), preview=invoice()).install(monkeypatch)
    req = billing.AddProductRequest(
        "precheck",
        "precheck_basic",
        (),
        True,
        str(uuid4()),
        expected_charge=billing.AddProductCharge("brl", 4500, None, None),
    )
    with pytest.raises(HTTPException) as exc:
        await billing.add_product_to_subscription(db_session, ent.tenant_id, req)
    assert (exc.value.status_code, exc.value.detail) == (422, "preview_quote_required")
    assert "UPDATE" not in stripe.kinds()


async def test_legacy_confirm_retry_has_stable_payload_without_proration_date(
    db_session, monkeypatch
):
    ent = await make_clinic(db_session, "secretaria_basico", None)
    stripe = FakeStripe(
        live=make_sub(*SEC_PRICES),
        preview=invoice(),
        post_error=billing.StripeApiError(409, "idempotency_key_in_use", None, None),
    ).install(monkeypatch)
    req = billing.AddProductRequest("precheck", "precheck_basic", (), True, str(uuid4()))
    for _ in range(2):
        with pytest.raises(HTTPException):
            await billing.add_product_to_subscription(db_session, ent.tenant_id, req)
    updates = [call for call in stripe.calls if call[0] == "UPDATE"]
    assert updates[0][2:] == updates[1][2:]
    assert "proration_date" not in updates[0][2]
