"""Cold-signup vertical tests: intent creation, Checkout Session, onboarding poll,
webhook provisioning, and the onboarding-token exchange.

Ground truth: services/signup.py, api/public_signup.py, api/auth.py (the two new
endpoints), and the `checkout.session.completed` / `metadata.kind == "signup_intent"`
branch in services/billing.apply_stripe_event. Reuses the Stripe test doubles from
tests/test_billing.py (real HMAC signatures, a fake httpx client) and the seeded client
fixture from tests/test_rbac.py (no separate DB setup needed).
"""

from types import SimpleNamespace

from brain_api.services import signup as signup_service
from tests.test_billing import _event, _install_fake_stripe_httpx, _post_webhook
from tests.test_rbac import ADMIN_EMAIL, ADMIN_PASSWORD, OWNER_A_EMAIL, _bearer, _token

# --- Signup-intent validation --------------------------------------------------------


async def test_signup_intent_rejects_unknown_catalog_id(client):
    resp = await client.post(
        "/public/signup-intents",
        json={
            "name": "Lead",
            "clinic_name": "Clinica X",
            "email": "lead1@example.com",
            "whatsapp_phone": "+5511999990000",
            "catalog_ids": ["not_a_real_catalog_id"],
        },
    )
    assert resp.status_code == 422


async def test_signup_intent_rejects_two_plans(client):
    resp = await client.post(
        "/public/signup-intents",
        json={
            "name": "Lead",
            "clinic_name": "Clinica X",
            "email": "lead2@example.com",
            "whatsapp_phone": "+5511999990000",
            "catalog_ids": ["secretaria_ferro", "precheck"],
        },
    )
    assert resp.status_code == 422


async def test_signup_intent_rejects_free_plan(client):
    resp = await client.post(
        "/public/signup-intents",
        json={
            "name": "Lead",
            "clinic_name": "Clinica X",
            "email": "lead3@example.com",
            "whatsapp_phone": "+5511999990000",
            "catalog_ids": ["free"],
        },
    )
    assert resp.status_code == 422


async def test_signup_intent_existing_email_conflict(client):
    """A user already registered with this email -> 409 (never let a duplicate reach
    the point of being provisioned after payment)."""
    resp = await client.post(
        "/public/signup-intents",
        json={
            "name": "Lead",
            "clinic_name": "Clinica X",
            "email": OWNER_A_EMAIL,
            "whatsapp_phone": "+5511999990000",
            "catalog_ids": ["secretaria_ferro"],
        },
    )
    assert resp.status_code == 409


async def test_signup_intent_honeypot_dropped(client):
    resp = await client.post(
        "/public/signup-intents",
        json={
            "name": "Bot",
            "clinic_name": "Bot Clinic",
            "email": "bot@example.com",
            "whatsapp_phone": "+5511999990000",
            "catalog_ids": ["secretaria_ferro"],
            "website": "http://spam.example",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["intent_id"] == "00000000-0000-0000-0000-000000000000"


async def test_signup_intent_happy_path(client):
    resp = await client.post(
        "/public/signup-intents",
        json={
            "name": "Dr. Lead",
            "clinic_name": "Clinica Nova",
            "email": "cold.lead@example.com",
            "whatsapp_phone": "+5511999990000",
            "catalog_ids": ["secretaria_ferro"],
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["intent_id"]


# --- Checkout session -----------------------------------------------------------------


async def _create_intent(client, *, email: str, catalog_ids: list[str]) -> str:
    resp = await client.post(
        "/public/signup-intents",
        json={
            "name": "Dr. Lead",
            "clinic_name": "Clinica Signup Test",
            "email": email,
            "whatsapp_phone": "+5511999990000",
            "catalog_ids": catalog_ids,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["intent_id"]


async def test_checkout_session_unknown_intent_404(client):
    resp = await client.post(
        "/public/checkout-sessions",
        json={"intent_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert resp.status_code == 404


async def test_checkout_session_happy_path(client, monkeypatch):
    intent_id = await _create_intent(
        client, email="checkout.happy@example.com", catalog_ids=["secretaria_ferro"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch,
        captured,
        {"id": "cs_signup_1", "url": "https://checkout.stripe.test/signup"},
    )

    resp = await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"checkout_url": "https://checkout.stripe.test/signup"}

    data = captured["data"]
    assert data["mode"] == "subscription"
    assert data["line_items[0][price]"] == "price_ferro"
    assert data["metadata[kind]"] == "signup_intent"
    assert data["metadata[signup_intent_id]"] == intent_id
    assert data["subscription_data[metadata][signup_intent_id]"] == intent_id
    assert data["client_reference_id"] == intent_id
    assert data["customer_email"] == "checkout.happy@example.com"
    assert data["phone_number_collection[enabled]"] == "true"
    assert "metadata[tenant_id]" not in data


async def test_checkout_session_not_pending_conflict(client, monkeypatch):
    intent_id = await _create_intent(
        client, email="checkout.conflict@example.com", catalog_ids=["secretaria_ferro"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_signup_2", "url": "https://checkout.stripe.test/x"}
    )
    first = await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    assert first.status_code == 200, first.text

    # Provision it (webhook), which flips status away from pending_payment.
    obj = {
        "customer": "cus_conflict",
        "subscription": "sub_conflict",
        "metadata": {"kind": "signup_intent", "signup_intent_id": intent_id},
    }
    webhook_resp = await _post_webhook(
        client, _event("evt_signup_conflict", "checkout.session.completed", obj)
    )
    assert webhook_resp.status_code == 200

    second = await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    assert second.status_code == 409


# --- Onboarding status: pending / ready / failed + token rotation ---------------------


async def test_onboarding_status_unknown_session_404(client):
    resp = await client.get("/public/onboarding-status", params={"session_id": "cs_unknown"})
    assert resp.status_code == 404


async def test_onboarding_status_pending_then_ready_with_token_rotation(client, monkeypatch):
    intent_id = await _create_intent(
        client, email="status.flow@example.com", catalog_ids=["secretaria_bronze_1"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_status_flow", "url": "https://checkout.stripe.test/y"}
    )
    checkout_resp = await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    assert checkout_resp.status_code == 200, checkout_resp.text

    pending = await client.get("/public/onboarding-status", params={"session_id": "cs_status_flow"})
    assert pending.status_code == 200
    assert pending.json() == {"status": "pending", "products": None, "onboarding_token": None}

    obj = {
        "customer": "cus_status_flow",
        "subscription": "sub_status_flow",
        "metadata": {"kind": "signup_intent", "signup_intent_id": intent_id},
    }
    webhook_resp = await _post_webhook(
        client, _event("evt_status_flow", "checkout.session.completed", obj)
    )
    assert webhook_resp.status_code == 200

    ready_1 = await client.get("/public/onboarding-status", params={"session_id": "cs_status_flow"})
    assert ready_1.status_code == 200
    body_1 = ready_1.json()
    assert body_1["status"] == "ready"
    assert body_1["products"] == {"secretaria": True, "precheck": False}
    assert body_1["onboarding_token"]

    ready_2 = await client.get("/public/onboarding-status", params={"session_id": "cs_status_flow"})
    body_2 = ready_2.json()
    assert body_2["onboarding_token"]
    # Every poll before redemption rotates the token.
    assert body_2["onboarding_token"] != body_1["onboarding_token"]

    # The FIRST (already-rotated) token no longer works.
    stale_exchange = await client.post(
        "/auth/exchange-onboarding-token", json={"token": body_1["onboarding_token"]}
    )
    assert stale_exchange.status_code == 401

    # The current token exchanges for a real session.
    exchange = await client.post(
        "/auth/exchange-onboarding-token", json={"token": body_2["onboarding_token"]}
    )
    assert exchange.status_code == 200, exchange.text
    token_body = exchange.json()
    assert token_body["access_token"]
    assert token_body["refresh_token"]

    me = await client.get("/auth/me", headers=_bearer(token_body["access_token"]))
    assert me.status_code == 200, me.text
    assert me.json()["user"]["email"] == "status.flow@example.com"

    # Once redeemed, onboarding_token stays null on every subsequent poll.
    after_use = await client.get(
        "/public/onboarding-status", params={"session_id": "cs_status_flow"}
    )
    after_body = after_use.json()
    assert after_body["status"] == "ready"
    assert after_body["onboarding_token"] is None

    # Re-exchanging the now-used token is rejected.
    reuse = await client.post(
        "/auth/exchange-onboarding-token", json={"token": body_2["onboarding_token"]}
    )
    assert reuse.status_code == 401
    assert reuse.json()["detail"] == "invalid_onboarding_token"


async def test_onboarding_status_expired_token_rejected(client, monkeypatch):
    intent_id = await _create_intent(
        client, email="status.expired@example.com", catalog_ids=["secretaria_ferro"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_status_expired", "url": "https://checkout.stripe.test/z"}
    )
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200

    obj = {
        "customer": "cus_expired",
        "subscription": "sub_expired",
        "metadata": {"kind": "signup_intent", "signup_intent_id": intent_id},
    }
    assert (
        await _post_webhook(client, _event("evt_expired", "checkout.session.completed", obj))
    ).status_code == 200

    # Force the token minted by THIS poll to be already expired.
    monkeypatch.setattr(
        signup_service,
        "get_settings",
        lambda: SimpleNamespace(ONBOARDING_TOKEN_EXPIRE_MINUTES=-5),
    )
    ready = await client.get(
        "/public/onboarding-status", params={"session_id": "cs_status_expired"}
    )
    token = ready.json()["onboarding_token"]
    assert token

    expired_exchange = await client.post("/auth/exchange-onboarding-token", json={"token": token})
    assert expired_exchange.status_code == 401


async def test_onboarding_status_failed_intent(client, monkeypatch):
    """A raced email conflict at provisioning time fails the intent (never raises); the
    webhook still 200s, and the poll surfaces `status: "failed"`."""
    intent_id = await _create_intent(
        client, email="race.conflict@example.com", catalog_ids=["secretaria_ferro"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_race", "url": "https://checkout.stripe.test/race"}
    )
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200

    # Simulate the race: something else registers this email before the webhook lands.
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    any_tenant_id = tenants[0]["id"]
    created = await client.post(
        "/admin/users",
        headers=_bearer(admin_token),
        json={
            "email": "race.conflict@example.com",
            "name": "Racer",
            "password": "racepass1",
            "role": "tenant_staff",
            "tenant_id": any_tenant_id,
        },
    )
    assert created.status_code == 201, created.text

    obj = {
        "customer": "cus_race",
        "subscription": "sub_race",
        "metadata": {"kind": "signup_intent", "signup_intent_id": intent_id},
    }
    webhook_resp = await _post_webhook(
        client, _event("evt_race", "checkout.session.completed", obj)
    )
    assert webhook_resp.status_code == 200, webhook_resp.text

    status_resp = await client.get("/public/onboarding-status", params={"session_id": "cs_race"})
    assert status_resp.status_code == 200
    assert status_resp.json() == {"status": "failed", "products": None, "onboarding_token": None}


# --- Webhook provisioning: full tenant/user/entitlement materialization ---------------


async def test_webhook_signup_intent_provisions_tenant_and_is_idempotent(client, monkeypatch):
    intent_id = await _create_intent(
        client,
        email="provision.full@example.com",
        catalog_ids=["secretaria_bronze_1", "multi_professional"],
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_provision_full", "url": "https://checkout.stripe.test/p"}
    )
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    before = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    before_names = [t["clinic_name"] for t in before]
    assert "Clinica Signup Test" not in before_names

    obj = {
        "customer": "cus_provision_full",
        "subscription": "sub_provision_full",
        "client_reference_id": intent_id,
        "metadata": {"kind": "signup_intent", "signup_intent_id": intent_id},
    }
    first = await _post_webhook(
        client, _event("evt_provision_full", "checkout.session.completed", obj)
    )
    assert first.status_code == 200
    assert first.json() == {"received": True, "duplicate": False}

    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    matches = [t for t in tenants if t["clinic_name"] == "Clinica Signup Test"]
    assert len(matches) == 1
    tenant_id = matches[0]["id"]

    ent = (
        await client.get(f"/admin/tenants/{tenant_id}/entitlements", headers=_bearer(admin_token))
    ).json()
    assert ent["plan"] == "secretaria_bronze_1"
    assert ent["secretaria_enabled"] is True
    assert ent["precheck_enabled"] is False
    assert ent["status"] == "active"
    assert ent["stripe_customer_id"] == "cus_provision_full"
    assert ent["stripe_subscription_id"] == "sub_provision_full"
    assert ent["addons"]["multi_professional"] is True
    # base (1) + one addon-active grant (1) = 2 — no quantity scaling on this path
    # (that only happens on the subscription.* events, not the signup-intent branch).
    assert ent["limits"]["professionals"] == 2
    assert ent["limits"]["reminders"] == 200

    # Idempotent: a redelivery of the SAME event must not create a second tenant.
    replay = await _post_webhook(
        client, _event("evt_provision_full", "checkout.session.completed", obj)
    )
    assert replay.status_code == 200
    assert replay.json() == {"received": True, "duplicate": True}

    tenants_after = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()[
        "items"
    ]
    matches_after = [t for t in tenants_after if t["clinic_name"] == "Clinica Signup Test"]
    assert len(matches_after) == 1


# --- Exchange endpoint: unknown/garbage token ------------------------------------------


async def test_exchange_onboarding_token_unknown_rejected(client):
    resp = await client.post("/auth/exchange-onboarding-token", json={"token": "not-a-real-token"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_onboarding_token"


# --- Set-password -----------------------------------------------------------------------


async def test_set_password_lets_provisioned_owner_login_with_new_password(client, monkeypatch):
    intent_id = await _create_intent(
        client, email="setpw.owner@example.com", catalog_ids=["secretaria_ferro"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_setpw", "url": "https://checkout.stripe.test/setpw"}
    )
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200

    obj = {
        "customer": "cus_setpw",
        "subscription": "sub_setpw",
        "metadata": {"kind": "signup_intent", "signup_intent_id": intent_id},
    }
    assert (
        await _post_webhook(client, _event("evt_setpw", "checkout.session.completed", obj))
    ).status_code == 200

    status_resp = await client.get("/public/onboarding-status", params={"session_id": "cs_setpw"})
    token = status_resp.json()["onboarding_token"]
    assert token

    exchange = await client.post("/auth/exchange-onboarding-token", json={"token": token})
    assert exchange.status_code == 200, exchange.text
    access_token = exchange.json()["access_token"]

    set_pw = await client.post(
        "/auth/set-password",
        headers=_bearer(access_token),
        json={"new_password": "newpass123"},
    )
    assert set_pw.status_code == 204

    login = await client.post(
        "/auth/token", json={"email": "setpw.owner@example.com", "password": "newpass123"}
    )
    assert login.status_code == 200, login.text


async def test_set_password_requires_auth(client):
    resp = await client.post("/auth/set-password", json={"new_password": "newpass123"})
    assert resp.status_code == 401


async def test_set_password_rejects_weak_password(client, monkeypatch):
    intent_id = await _create_intent(
        client, email="setpw.weak@example.com", catalog_ids=["secretaria_ferro"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_setpw_weak", "url": "https://checkout.stripe.test/w"}
    )
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200
    obj = {
        "customer": "cus_setpw_weak",
        "subscription": "sub_setpw_weak",
        "metadata": {"kind": "signup_intent", "signup_intent_id": intent_id},
    }
    assert (
        await _post_webhook(client, _event("evt_setpw_weak", "checkout.session.completed", obj))
    ).status_code == 200
    token = (
        await client.get("/public/onboarding-status", params={"session_id": "cs_setpw_weak"})
    ).json()["onboarding_token"]
    access_token = (
        await client.post("/auth/exchange-onboarding-token", json={"token": token})
    ).json()["access_token"]

    resp = await client.post(
        "/auth/set-password",
        headers=_bearer(access_token),
        json={"new_password": "12345678"},
    )
    assert resp.status_code == 422


# --- Rate limiting ----------------------------------------------------------------------


async def test_signup_intent_rate_limited(client, monkeypatch):
    from brain_api.api import public_signup
    from brain_api.core.ratelimit import SlidingWindowLimiter

    monkeypatch.setattr(public_signup, "_limiter", SlidingWindowLimiter("t", lambda: 1))

    payload = {
        "name": "Lead",
        "clinic_name": "Clinica Rate",
        "whatsapp_phone": "+5511999990000",
        "catalog_ids": ["secretaria_ferro"],
    }
    first = await client.post(
        "/public/signup-intents", json={**payload, "email": "rate1@example.com"}
    )
    assert first.status_code == 201
    second = await client.post(
        "/public/signup-intents", json={**payload, "email": "rate2@example.com"}
    )
    assert second.status_code == 429
