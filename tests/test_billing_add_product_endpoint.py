"""POST /billing/add-product over HTTP (TASK C, spec §5.5): contract, gates, error shapes."""

from uuid import uuid4

import pytest

from brain_api.services import onboarding_sync
from tests.billing_fakes import (
    SEC_PRICES,
    FakeStripe,
    install_fake_settings,
    make_invoice,
    make_sub,
    set_entitlement,
    snapshot,
)
from tests.test_billing import _tenant_ids
from tests.test_billing_role_gate import PASSWORD, _add_user
from tests.test_rbac import CLINIC_B, OWNER_B_EMAIL, OWNER_B_PASSWORD, _bearer, _token

PREVIEW = make_invoice(proration=False)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    install_fake_settings(monkeypatch)

    async def no_bridges(session, tenant_id):
        return None

    monkeypatch.setattr(onboarding_sync, "ensure_products_provisioned", no_bridges)


async def _precheck_clinic(client) -> str:
    tenant_b = (await _tenant_ids(client))[CLINIC_B]
    await set_entitlement(
        tenant_b,
        plan="precheck_basic",
        status="active",
        precheck_enabled=True,
        secretaria_enabled=False,
        stripe_customer_id="cus_1",
        stripe_subscription_id="sub_1",
        manual_products=[],
    )
    return tenant_b


async def _post(
    client, body: dict, *, key: str | None = None, email=OWNER_B_EMAIL, password=OWNER_B_PASSWORD
):
    token = await _token(client, email, password)
    headers = _bearer(token)
    if key is not None:
        headers["Idempotency-Key"] = key
    return await client.post("/billing/add-product", headers=headers, json=body)


async def test_preview_returns_the_charge_and_no_entitlement(client, monkeypatch):
    await _precheck_clinic(client)
    FakeStripe(live=make_sub("price_pc_basic"), preview=PREVIEW).install(monkeypatch)
    resp = await _post(client, {"product": "secretaria", "confirm": False})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "status": "preview",
        "product": "secretaria",
        "charge": {
            "currency": "brl",
            "amount_due_now_cents": 0,
            "next_invoice_cents": None,
            "next_invoice_date": None,
        },
        "entitlement": None,
        "return_query": None,
        "quote_token": resp.json()["quote_token"],
    }


async def test_confirm_adds_the_product_and_returns_the_updated_entitlement(client, monkeypatch):
    tenant_b = await _precheck_clinic(client)
    updated = make_sub(
        "price_pc_basic", *SEC_PRICES, latest_invoice={"amount_paid": 0, "currency": "brl"}
    )
    stripe = FakeStripe(live=make_sub("price_pc_basic"), updated=updated).install(monkeypatch)
    key = str(uuid4())

    resp = await _post(client, {"product": "secretaria", "confirm": True}, key=key)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "added"
    assert body["entitlement"]["products"] == {"precheck": True, "secretaria": True}
    assert body["entitlement"]["plan"] == "secretaria_basico"
    assert body["entitlement"]["precheck_plan"] == "precheck_basic"
    assert stripe.call("UPDATE")[3] == f"add-product:{tenant_b}:{key}"


async def test_already_present_is_200_with_the_entitlement(client, monkeypatch):
    await _precheck_clinic(client)
    FakeStripe(live=make_sub("price_pc_basic", *SEC_PRICES)).install(monkeypatch)
    resp = await _post(client, {"product": "secretaria", "confirm": True}, key=str(uuid4()))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "already_present"
    assert resp.json()["entitlement"]["products"]["secretaria"] is True


@pytest.mark.parametrize(
    ("key", "detail"), [(None, "idempotency_key_required"), ("nope", "invalid_idempotency_key")]
)
async def test_confirm_needs_a_valid_idempotency_key(client, monkeypatch, key, detail):
    await _precheck_clinic(client)
    stripe = FakeStripe(live=make_sub("price_pc_basic")).install(monkeypatch)
    resp = await _post(client, {"product": "secretaria", "confirm": True}, key=key)
    assert resp.status_code == 422
    assert resp.json() == {"detail": detail}
    assert stripe.calls == []


@pytest.mark.parametrize(
    "extra",
    [{"subscription_id": "sub_evil"}, {"customer_id": "cus_evil"}, {"tenant_id": str(uuid4())}],
)
async def test_the_body_never_names_a_subscription_customer_or_tenant(client, monkeypatch, extra):
    await _precheck_clinic(client)
    stripe = FakeStripe(live=make_sub("price_pc_basic")).install(monkeypatch)
    resp = await _post(client, {"product": "secretaria", "confirm": False, **extra})
    assert resp.status_code == 422
    assert stripe.calls == []


async def test_the_launch_gate_answers_403_before_any_stripe_call(client, monkeypatch):
    await _precheck_clinic(client)
    install_fake_settings(monkeypatch, BILLING_ADD_SECRETARIA_ENABLED=False)
    stripe = FakeStripe(live=make_sub("price_pc_basic")).install(monkeypatch)
    resp = await _post(client, {"product": "secretaria", "confirm": False})
    assert resp.status_code == 403
    assert resp.json() == {"detail": "product_not_launched"}
    assert stripe.calls == []


async def test_a_clinic_without_a_subscription_is_told_to_use_checkout(client, monkeypatch):
    tenant_b = (await _tenant_ids(client))[CLINIC_B]
    await set_entitlement(
        tenant_b,
        plan="precheck_basic",
        status="active",
        precheck_enabled=True,
        manual_products=["precheck"],
    )
    stripe = FakeStripe().install(monkeypatch)
    resp = await _post(client, {"product": "secretaria", "confirm": False})
    assert (resp.status_code, resp.json()) == (409, {"detail": "no_active_subscription"})
    assert stripe.calls == []


@pytest.mark.parametrize("role", ["secretary", "doctor"])
async def test_secretary_and_plain_doctor_are_refused_before_anything(client, monkeypatch, role):
    tenant_b = await _precheck_clinic(client)
    await _add_user(tenant_b, f"{role}@example.com", role)
    stripe = FakeStripe(live=make_sub("price_pc_basic")).install(monkeypatch)
    before = await snapshot(tenant_b)
    resp = await _post(
        client,
        {"product": "secretaria", "confirm": False},
        email=f"{role}@example.com",
        password=PASSWORD,
    )
    assert (resp.status_code, resp.json()) == (403, {"detail": "billing_role_required"})
    assert stripe.calls == []
    assert await snapshot(tenant_b) == before


async def test_a_card_declined_is_402_and_leaves_the_row_untouched(client, monkeypatch):
    from brain_api.services.billing import StripeApiError

    tenant_b = await _precheck_clinic(client)
    error = StripeApiError(402, "card_declined", "card_error", "Declined.")
    FakeStripe(live=make_sub("price_pc_basic"), post_error=error).install(monkeypatch)
    before = await snapshot(tenant_b)
    resp = await _post(client, {"product": "secretaria", "confirm": True}, key=str(uuid4()))
    assert (resp.status_code, resp.json()) == (402, {"detail": "payment_failed"})
    assert await snapshot(tenant_b) == before


async def test_confirm_with_return_to_console_returns_the_query_built_on_the_server(
    client, monkeypatch
):
    await _precheck_clinic(client)
    updated = make_sub(
        "price_pc_basic", *SEC_PRICES, latest_invoice={"amount_paid": 0, "currency": "brl"}
    )
    FakeStripe(live=make_sub("price_pc_basic"), updated=updated).install(monkeypatch)

    resp = await _post(
        client, {"product": "secretaria", "confirm": True, "return_to": "console"}, key=str(uuid4())
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "added"
    assert resp.json()["return_query"] == "origem=console"  # secretarIA does not carry PreCheck


async def test_already_present_with_return_to_also_returns_the_query(client, monkeypatch):
    await _precheck_clinic(client)
    FakeStripe(live=make_sub("price_pc_basic", *SEC_PRICES)).install(monkeypatch)

    resp = await _post(
        client, {"product": "secretaria", "confirm": True, "return_to": "console"}, key=str(uuid4())
    )

    assert resp.json()["status"] == "already_present"
    assert resp.json()["return_query"] == "origem=console"


async def test_preview_never_returns_a_return_query(client, monkeypatch):
    await _precheck_clinic(client)
    FakeStripe(live=make_sub("price_pc_basic"), preview=PREVIEW).install(monkeypatch)

    resp = await _post(client, {"product": "secretaria", "confirm": False, "return_to": "console"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["return_query"] is None


async def test_without_return_to_there_is_no_return_query(client, monkeypatch):
    await _precheck_clinic(client)
    FakeStripe(live=make_sub("price_pc_basic", *SEC_PRICES)).install(monkeypatch)

    resp = await _post(client, {"product": "secretaria", "confirm": True}, key=str(uuid4()))

    assert resp.json()["return_query"] is None


@pytest.mark.parametrize(
    "hostile", ["https://evil.com", "//evil.com", "Console", "console&x=1", "console" * 50]
)
async def test_hostile_return_to_is_422_before_stripe(client, monkeypatch, hostile):
    tenant_b = await _precheck_clinic(client)
    stripe = FakeStripe(live=make_sub("price_pc_basic")).install(monkeypatch)
    before = await snapshot(tenant_b)

    resp = await _post(
        client, {"product": "secretaria", "confirm": True, "return_to": hostile}, key=str(uuid4())
    )

    assert resp.status_code == 422, resp.text
    assert stripe.calls == []
    assert await snapshot(tenant_b) == before


async def test_a_client_cannot_name_a_url_or_path_to_return_to(client, monkeypatch):
    await _precheck_clinic(client)
    stripe = FakeStripe(live=make_sub("price_pc_basic")).install(monkeypatch)

    resp = await _post(
        client,
        {
            "product": "secretaria",
            "confirm": True,
            "return_to": "console",
            "return_url": "https://evil.com",
        },
        key=str(uuid4()),
    )

    assert resp.status_code == 422, resp.text  # extra="forbid"
    assert stripe.calls == []


async def test_preview_quote_confirm_contract_and_forbidden_client_date(client, monkeypatch):
    await _precheck_clinic(client)
    updated = make_sub("price_pc_basic", *SEC_PRICES)
    stripe = FakeStripe(live=make_sub("price_pc_basic"), updated=updated).install(monkeypatch)
    shown = await _post(client, {"product": "secretaria"})
    assert shown.status_code == 200
    body = shown.json()
    assert isinstance(body["quote_token"], str)
    confirmed = await _post(
        client,
        {
            "product": "secretaria",
            "confirm": True,
            "expected_charge": body["charge"],
            "quote_token": body["quote_token"],
        },
        key=str(uuid4()),
    )
    assert confirmed.status_code == 200, confirmed.text
    previews = [call for call in stripe.calls if call[0] == "PREVIEW"]
    update = stripe.call("UPDATE")
    assert previews[0][2]["subscription_details[proration_date]"] == update[2]["proration_date"]
    stripe.calls.clear()
    refused = await _post(client, {"product": "secretaria", "proration_date": 1})
    assert refused.status_code == 422
    assert stripe.calls == []
