"""Stripe billing vertical tests (webhook apply + checkout/portal endpoints).

Ground truth: services/billing.py (price map, checkout/portal, apply_stripe_event) and
api/billing.py (routes + webhook signature verification). Webhook signatures are built
for REAL (HMAC-SHA256 over `f"{ts}.{body}"`, matching stripe.Webhook.construct_event's
own verification), so these exercise the actual signature check, not a mocked one.
STRIPE_SECRET_KEY stays unset in tests (conftest) so no code path can ever reach the
real Stripe API; checkout/portal happy-path tests monkeypatch settings + httpx instead,
mirroring tests/test_doctor_secretaria.py's fake-httpx pattern.
"""

import hashlib
import hmac
import json
import os
import time
from types import SimpleNamespace

from brain_api.services import billing as billing_service
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

# Reuse the EXACT same env strings conftest set — `_parse_price_map` is lru_cached by the
# raw string, so a different-but-equal JSON literal would still be a fresh cache slot.
WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
PRICE_MAP_JSON = os.environ["STRIPE_PRICE_MAP"]


def _sig_header(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """A real Stripe-Signature header stripe.Webhook.construct_event will accept."""
    ts = int(time.time())
    signed_payload = f"{ts}.{body.decode()}"
    sig = hmac.new(secret.encode(), signed_payload.encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _event(event_id: str, event_type: str, obj: dict) -> dict:
    return {
        "id": event_id,
        "type": event_type,
        "data": {"object": obj},
        "api_version": "2024-06-20",
        "object": "event",
    }


async def _post_webhook(client, event: dict, *, sig: str | None = None):
    body = json.dumps(event).encode()
    header = sig if sig is not None else _sig_header(body)
    return await client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": header, "Content-Type": "application/json"},
    )


async def _tenant_ids(client) -> dict[str, str]:
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()
    return {t["clinic_name"]: t["id"] for t in tenants["items"]}


async def _admin_entitlements(client, tenant_id: str) -> dict:
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    resp = await client.get(
        f"/admin/tenants/{tenant_id}/entitlements", headers=_bearer(admin_token)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


def _install_fake_stripe_httpx(monkeypatch, captured: dict, response_body: dict) -> None:
    """Point services.billing at fake Stripe settings + a recording fake httpx client."""

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        async def post(self, path: str, data=None, auth=None) -> _FakeResponse:
            captured["path"] = path
            captured["data"] = data
            captured["auth"] = auth
            return _FakeResponse(200, response_body)

    fake_settings = SimpleNamespace(
        STRIPE_SECRET_KEY="sk_test",
        STRIPE_API_BASE="https://api.stripe.test",
        STRIPE_TIMEOUT_SECONDS=15.0,
        STRIPE_PRICE_MAP=PRICE_MAP_JSON,
        STRIPE_CHECKOUT_SUCCESS_URL="http://localhost:3000/app?checkout=success",
        STRIPE_CHECKOUT_CANCEL_URL="http://localhost:3000/app?checkout=cancelled",
        STRIPE_PORTAL_RETURN_URL="http://localhost:3000/app",
        # Onboarding round (CONTRACT_onboarding_v1.md §9): create_checkout_session /
        # create_checkout_session_for_intent both unconditionally read this now (0/off
        # is a no-op) via services.billing._apply_trial — every fake settings object
        # standing in for get_settings() needs it, or that call 500s with an
        # AttributeError. tests/test_billing_phase1.py exercises the non-zero case with
        # its own extended fake settings.
        STRIPE_TRIAL_PERIOD_DAYS=0,
    )
    monkeypatch.setattr(billing_service, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(billing_service.httpx, "AsyncClient", _FakeClient)


# --- Webhook: signature verification ----------------------------------------


async def test_webhook_bad_signature_rejected(client):
    body = json.dumps(_event("evt_bad1", "checkout.session.completed", {})).encode()
    bad_sig = await client.post(
        "/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=1,v1=deadbeef"}
    )
    assert bad_sig.status_code == 400

    missing_sig = await client.post("/webhooks/stripe", content=body)
    assert missing_sig.status_code == 400


async def test_webhook_valid_signature_accepted(client):
    event = _event(
        "evt_valid1", "customer.subscription.updated", {"status": "trialing", "id": "sub_x"}
    )
    resp = await _post_webhook(client, event)
    assert resp.status_code == 200
    assert resp.json() == {"received": True, "duplicate": False}


async def test_webhook_secret_unset_rejects_all(client, monkeypatch):
    """Even a signature valid against the REAL secret is rejected when the configured
    secret is empty (fail closed on an unconfigured webhook, api/billing.py)."""
    monkeypatch.setattr(
        "brain_api.api.billing.get_settings",
        lambda: SimpleNamespace(STRIPE_WEBHOOK_SECRET=""),
    )
    body = json.dumps(_event("evt_nosecret", "checkout.session.completed", {})).encode()
    resp = await client.post(
        "/webhooks/stripe", content=body, headers={"Stripe-Signature": _sig_header(body)}
    )
    assert resp.status_code == 400


# --- Webhook: checkout.session.completed links Stripe ids -------------------


async def test_checkout_completed_upserts_stripe_ids(client):
    ids = await _tenant_ids(client)
    tenant_b = ids[CLINIC_B]
    obj = {"customer": "cus_1", "subscription": "sub_1", "metadata": {"tenant_id": tenant_b}}
    resp = await _post_webhook(client, _event("evt_checkout_1", "checkout.session.completed", obj))
    assert resp.status_code == 200
    assert resp.json() == {"received": True, "duplicate": False}

    ent = await _admin_entitlements(client, tenant_b)
    assert ent["stripe_customer_id"] == "cus_1"
    assert ent["stripe_subscription_id"] == "sub_1"


# --- Webhook: full recompute on subscription.created -------------------------


async def test_subscription_created_full_recompute(client):
    ids = await _tenant_ids(client)
    tenant_b = ids[CLINIC_B]
    now = int(time.time())
    obj = {
        "id": "sub_1",
        "customer": "cus_1",
        "status": "active",
        "current_period_start": now,
        "current_period_end": now + 30 * 86400,
        "items": {"data": [{"price": {"id": "price_combo"}, "quantity": 1}]},
        "metadata": {"tenant_id": tenant_b},
    }
    resp = await _post_webhook(
        client, _event("evt_sub_created_1", "customer.subscription.created", obj)
    )
    assert resp.status_code == 200

    ent = await _admin_entitlements(client, tenant_b)
    assert ent["plan"] == "complete_clinic_combo"
    assert ent["precheck_enabled"] is True
    assert ent["secretaria_enabled"] is True
    assert ent["addons"]["reactivation_pack"] is True
    assert ent["addons"]["verified_identity"] is True
    # reminders is metering-only now (2026-07-22) -- no plan/add-on grants a base quota.
    assert ent["limits"]["reminders"] == 0
    assert ent["status"] == "active"
    assert ent["period_start"] is not None
    assert ent["period_end"] is not None

    owner_b_token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    ent_out = (await client.get("/entitlements", headers=_bearer(owner_b_token))).json()
    assert ent_out["products"] == {"precheck": True, "secretaria": True}


# --- Webhook: quantity scaling -------------------------------------------------


async def test_quantity_scaling_multi_professional(client):
    ids = await _tenant_ids(client)
    tenant_b = ids[CLINIC_B]
    now = int(time.time())
    obj = {
        "id": "sub_qty",
        "customer": "cus_qty",
        "status": "active",
        "current_period_start": now,
        "current_period_end": now + 30 * 86400,
        "items": {
            "data": [
                {"price": {"id": "price_ferro"}, "quantity": 1},
                {"price": {"id": "price_multipro"}, "quantity": 3},
            ]
        },
        "metadata": {"tenant_id": tenant_b},
    }
    resp = await _post_webhook(client, _event("evt_sub_qty", "customer.subscription.updated", obj))
    assert resp.status_code == 200

    ent = await _admin_entitlements(client, tenant_b)
    assert ent["plan"] == "secretaria_basico"
    assert ent["precheck_enabled"] is False
    # Base grant (1) + one addon-active grant (1) + (qty - 1) additional units (2) = 4.
    assert ent["limits"]["professionals"] == 4


# --- Webhook: status transitions ---------------------------------------------


async def test_status_transitions(client):
    ids = await _tenant_ids(client)
    tenant_b = ids[CLINIC_B]
    now = int(time.time())

    link_obj = {
        "customer": "cus_seq",
        "subscription": "sub_seq",
        "metadata": {"tenant_id": tenant_b},
    }
    linked = await _post_webhook(
        client, _event("evt_seq_link", "checkout.session.completed", link_obj)
    )
    assert linked.status_code == 200

    sub_obj = {
        "id": "sub_seq",
        "customer": "cus_seq",
        "status": "trialing",
        "current_period_start": now,
        "current_period_end": now + 86400,
        "items": {"data": [{"price": {"id": "price_ferro"}, "quantity": 1}]},
        "metadata": {"tenant_id": tenant_b},
    }
    created = await _post_webhook(
        client, _event("evt_seq_created", "customer.subscription.created", sub_obj)
    )
    assert created.status_code == 200
    assert (await _admin_entitlements(client, tenant_b))["status"] == "trialing"

    # No metadata: resolved via the linked stripe_customer_id.
    failed_obj = {"customer": "cus_seq"}
    failed = await _post_webhook(
        client, _event("evt_seq_failed", "invoice.payment_failed", failed_obj)
    )
    assert failed.status_code == 200
    assert (await _admin_entitlements(client, tenant_b))["status"] == "past_due"

    paid_obj = {"customer": "cus_seq"}
    paid = await _post_webhook(client, _event("evt_seq_paid", "invoice.paid", paid_obj))
    assert paid.status_code == 200
    assert (await _admin_entitlements(client, tenant_b))["status"] == "active"

    deleted_obj = {"customer": "cus_seq", "metadata": {"tenant_id": tenant_b}}
    deleted = await _post_webhook(
        client, _event("evt_seq_deleted", "customer.subscription.deleted", deleted_obj)
    )
    assert deleted.status_code == 200
    ent = await _admin_entitlements(client, tenant_b)
    assert ent["status"] == "canceled"
    assert ent["precheck_enabled"] is False
    assert ent["secretaria_enabled"] is False

    unpaid_obj = {
        "id": "sub_seq",
        "customer": "cus_seq",
        "status": "unpaid",
        "current_period_start": now,
        "current_period_end": now + 86400,
        "items": {"data": [{"price": {"id": "price_ferro"}, "quantity": 1}]},
        "metadata": {"tenant_id": tenant_b},
    }
    unpaid = await _post_webhook(
        client, _event("evt_seq_unpaid", "customer.subscription.updated", unpaid_obj)
    )
    assert unpaid.status_code == 200
    assert (await _admin_entitlements(client, tenant_b))["status"] == "inactive"


# --- Webhook: idempotency -----------------------------------------------------


async def test_idempotent_replay_does_not_reapply(client):
    ids = await _tenant_ids(client)
    tenant_b = ids[CLINIC_B]
    now = int(time.time())

    sub_obj = {
        "id": "sub_idem",
        "customer": "cus_idem",
        "status": "active",
        "current_period_start": now,
        "current_period_end": now + 86400,
        "items": {"data": [{"price": {"id": "price_ferro"}, "quantity": 1}]},
        "metadata": {"tenant_id": tenant_b},
    }
    created_event = _event("evt_idem_created", "customer.subscription.created", sub_obj)
    first = await _post_webhook(client, created_event)
    assert first.status_code == 200
    assert first.json() == {"received": True, "duplicate": False}
    assert (await _admin_entitlements(client, tenant_b))["secretaria_enabled"] is True

    deleted_obj = {"customer": "cus_idem", "metadata": {"tenant_id": tenant_b}}
    deleted = await _post_webhook(
        client, _event("evt_idem_deleted", "customer.subscription.deleted", deleted_obj)
    )
    assert deleted.status_code == 200
    ent = await _admin_entitlements(client, tenant_b)
    assert ent["status"] == "canceled"
    assert ent["secretaria_enabled"] is False

    # Replay the EXACT same (already-processed) event id/body — must be a no-op.
    replay = await _post_webhook(client, created_event)
    assert replay.status_code == 200
    assert replay.json() == {"received": True, "duplicate": True}

    ent_after = await _admin_entitlements(client, tenant_b)
    assert ent_after["status"] == "canceled"
    assert ent_after["secretaria_enabled"] is False


# --- Webhook: unknown price / no recognized plan ------------------------------


async def test_unknown_price_ignored_no_plan_change(client):
    ids = await _tenant_ids(client)
    tenant_b = ids[CLINIC_B]
    now = int(time.time())

    setup_obj = {
        "id": "sub_np",
        "customer": "cus_np",
        "status": "active",
        "current_period_start": now,
        "current_period_end": now + 86400,
        "items": {"data": [{"price": {"id": "price_ferro"}, "quantity": 1}]},
        "metadata": {"tenant_id": tenant_b},
    }
    setup = await _post_webhook(
        client, _event("evt_np_setup", "customer.subscription.created", setup_obj)
    )
    assert setup.status_code == 200
    before = await _admin_entitlements(client, tenant_b)
    assert before["plan"] == "secretaria_basico"

    later = now + 3600
    unknown_obj = {
        "id": "sub_np",
        "customer": "cus_np",
        "status": "active",
        "current_period_start": later,
        "current_period_end": later + 86400,
        "items": {"data": [{"price": {"id": "price_totally_unknown"}, "quantity": 1}]},
        "metadata": {"tenant_id": tenant_b},
    }
    updated = await _post_webhook(
        client, _event("evt_np_update", "customer.subscription.updated", unknown_obj)
    )
    assert updated.status_code == 200

    after = await _admin_entitlements(client, tenant_b)
    assert after["plan"] == "secretaria_basico"  # unchanged: no recognized plan price
    assert after["status"] == "active"
    assert after["period_start"] is not None
    assert after["period_end"] is not None


# --- Checkout / portal endpoints ----------------------------------------------


async def test_checkout_without_stripe_key_returns_503(client):
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/billing/checkout", headers=_bearer(token), json={"plan": "secretaria_basico"}
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "billing_not_configured"


async def test_checkout_validation_errors(client):
    """The "known but unassignable (reserved slot)" 422 case (e.g. the old
    secretaria_bronze_2 reserved slot) no longer applies -- the reserved-slot concept was
    retired 2026-07-22 along with the tier ladder; every catalog plan is assignable now."""
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    headers = _bearer(token)

    unknown_plan = await client.post(
        "/billing/checkout", headers=headers, json={"plan": "not_a_real_plan"}
    )
    assert unknown_plan.status_code == 422

    unknown_addon = await client.post(
        "/billing/checkout",
        headers=headers,
        json={"plan": "secretaria_basico", "addons": ["not_a_real_addon"]},
    )
    assert unknown_addon.status_code == 422

    price_missing = await client.post("/billing/checkout", headers=headers, json={"plan": "free"})
    assert price_missing.status_code == 503
    assert price_missing.json()["detail"] == "price_not_configured:free"


async def test_checkout_happy_path(client, monkeypatch):
    ids = await _tenant_ids(client)
    tenant_a = ids[CLINIC_A]
    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": "https://checkout.stripe.test/xyz"})

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/billing/checkout", headers=_bearer(token), json={"plan": "secretaria_basico"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": "https://checkout.stripe.test/xyz"}

    data = captured["data"]
    assert data["mode"] == "subscription"
    assert data["line_items[0][price]"] == "price_ferro"
    assert data["metadata[tenant_id]"] == tenant_a
    assert data["subscription_data[metadata][tenant_id]"] == tenant_a


async def test_portal_requires_billing_account_then_succeeds(client, monkeypatch):
    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    no_account = await client.post("/billing/portal", headers=_bearer(token))
    assert no_account.status_code == 409

    ids = await _tenant_ids(client)
    tenant_a = ids[CLINIC_A]
    link_obj = {
        "customer": "cus_portal",
        "subscription": "sub_portal",
        "metadata": {"tenant_id": tenant_a},
    }
    linked = await _post_webhook(
        client, _event("evt_portal_link", "checkout.session.completed", link_obj)
    )
    assert linked.status_code == 200

    captured: dict = {}
    _install_fake_stripe_httpx(monkeypatch, captured, {"url": "https://billing.stripe.test/portal"})

    resp = await client.post("/billing/portal", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": "https://billing.stripe.test/portal"}
    assert captured["data"]["customer"] == "cus_portal"


async def test_checkout_requires_tenant_scoped_token(client):
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    admin_resp = await client.post(
        "/billing/checkout", headers=_bearer(admin_token), json={"plan": "secretaria_ferro"}
    )
    assert admin_resp.status_code == 409

    noauth_resp = await client.post("/billing/checkout", json={"plan": "secretaria_ferro"})
    assert noauth_resp.status_code == 401


# --- Price map: legacy alias keys ---------------------------------------------


def test_price_map_accepts_secretaria_ferro_alias():
    """A not-yet-migrated deployed STRIPE_PRICE_MAP may still key the secretarIA price as
    "secretaria_ferro" (its id before the 2026-07-22 tier-ladder retirement); parsing
    must normalize it to secretaria_basico so both lookup directions (price_id_for at
    checkout, catalog_id_for_price in the webhook recompute) resolve to the current
    catalog plan."""
    parsed = billing_service._parse_price_map(
        '{"secretaria_ferro": "price_alias_test", "precheck": "price_pc_test"}'
    )
    assert parsed == {
        "secretaria_basico": "price_alias_test",
        "precheck": "price_pc_test",
    }
