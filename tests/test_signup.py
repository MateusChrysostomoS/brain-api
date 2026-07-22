"""Cold-signup vertical tests: register-at-first-card, Checkout Session, onboarding poll,
webhook ACTIVATION, and the onboarding-token exchange.

Ground truth: services/signup.py (`register_signup` — the sole tenant/user/inert-entitlement
writer — + `provision_tenant_from_intent` — the sole entitlement-ACTIVATION writer +
`attach_intake`), api/public_signup.py, api/onboarding.py (POST /doctor/onboarding/intake),
api/auth.py (the token exchanges), and the `checkout.session.completed` /
`metadata.kind == "signup_intent"` branch in services/billing.apply_stripe_event. Reuses
the Stripe test doubles from tests/test_billing.py (real HMAC signatures, a fake httpx
client) and the seeded client fixture from tests/test_rbac.py (no separate DB setup needed).
"""

from types import SimpleNamespace

from brain_api.models import Tenant
from brain_api.schemas.signup import IntakeIn, SignupIntentCreate
from brain_api.services import onboarding, signup as signup_service
from tests.test_billing import _event, _install_fake_stripe_httpx, _post_webhook
from tests.test_rbac import ADMIN_EMAIL, ADMIN_PASSWORD, OWNER_A_EMAIL, _bearer, _token

# A valid registration password (>= 8 chars, at least one letter and one digit — the
# schemas.signup.SignupIntentCreate policy, shared with SetPasswordIn/AdminUserCreateIn).
SIGNUP_PASSWORD = "signup123"


def _register_body(**overrides) -> dict:
    """A complete, valid `POST /public/signup-intents` body; override any field per test."""
    body = {
        "name": "Dr. Lead",
        "clinic_name": "Clinica Signup Test",
        "email": "cold.lead@example.com",
        "whatsapp_phone": "+5511999990000",
        "password": SIGNUP_PASSWORD,
        "catalog_ids": ["secretaria_basico"],
    }
    body.update(overrides)
    return body


# --- Registration validation ---------------------------------------------------------


async def test_signup_intent_rejects_unknown_catalog_id(client):
    resp = await client.post(
        "/public/signup-intents",
        json=_register_body(email="lead1@example.com", catalog_ids=["not_a_real_catalog_id"]),
    )
    assert resp.status_code == 422


async def test_signup_intent_rejects_two_plans(client):
    resp = await client.post(
        "/public/signup-intents",
        json=_register_body(email="lead2@example.com", catalog_ids=["secretaria_basico", "precheck"]),
    )
    assert resp.status_code == 422


async def test_signup_intent_rejects_free_plan(client):
    resp = await client.post(
        "/public/signup-intents",
        json=_register_body(email="lead3@example.com", catalog_ids=["free"]),
    )
    assert resp.status_code == 422


async def test_signup_intent_rejects_weak_password(client):
    """A purely-numeric (or too-short) password is rejected at registration (422), mirroring
    the SetPasswordIn / AdminUserCreateIn composition policy."""
    resp = await client.post(
        "/public/signup-intents",
        json=_register_body(email="weakpw@example.com", password="12345678"),
    )
    assert resp.status_code == 422


async def test_signup_intent_requires_password(client):
    """Password is now mandatory at the first card (the whole point of the split)."""
    body = _register_body(email="nopw@example.com")
    del body["password"]
    resp = await client.post("/public/signup-intents", json=body)
    assert resp.status_code == 422


async def test_signup_intent_existing_email_conflict(client):
    """A user already registered with this email -> 409 (never a second account)."""
    resp = await client.post(
        "/public/signup-intents",
        json=_register_body(email=OWNER_A_EMAIL),
    )
    assert resp.status_code == 409


async def test_signup_intent_honeypot_dropped(client):
    resp = await client.post(
        "/public/signup-intents",
        json=_register_body(
            name="Bot", clinic_name="Bot Clinic", email="bot@example.com",
            website="http://spam.example",
        ),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["intent_id"] == "00000000-0000-0000-0000-000000000000"
    # The honeypot response carries an empty (never-usable) session and creates nothing.
    assert body["session"]["access_token"] == ""


async def test_signup_intent_happy_path_registers_and_returns_session(client):
    resp = await client.post(
        "/public/signup-intents",
        json=_register_body(
            name="Dr. Lead", clinic_name="Clinica Nova", email="cold.lead@example.com"
        ),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["intent_id"]
    # A real session is returned — same shape login produces.
    assert body["session"]["access_token"]
    assert body["session"]["refresh_token"]
    # The session works immediately (the owner is logged in right after the first card).
    me = await client.get("/auth/me", headers=_bearer(body["session"]["access_token"]))
    assert me.status_code == 200, me.text
    assert me.json()["user"]["email"] == "cold.lead@example.com"
    assert me.json()["tenant"]["clinic_name"] == "Clinica Nova"


async def test_register_owner_can_login_immediately_before_payment(client):
    """The core fix: after the first card, the owner can log in normally with the password
    they chose — WITHOUT ever finishing the wizard or paying."""
    resp = await client.post(
        "/public/signup-intents",
        json=_register_body(email="login.now@example.com"),
    )
    assert resp.status_code == 201, resp.text

    login = await client.post(
        "/auth/token", json={"email": "login.now@example.com", "password": SIGNUP_PASSWORD}
    )
    assert login.status_code == 200, login.text
    assert login.json()["access_token"]


async def test_registered_but_unpaid_tenant_is_not_entitled(client):
    """A freshly registered, unpaid tenant resolves cleanly to a coherent "nothing
    purchased yet" entitlement (both products off, status inactive) — never a 404/500 —
    so the /app NoEntitlementsPanel renders."""
    resp = await client.post(
        "/public/signup-intents",
        json=_register_body(email="not.entitled@example.com"),
    )
    assert resp.status_code == 201, resp.text
    access_token = resp.json()["session"]["access_token"]

    ent = await client.get("/entitlements", headers=_bearer(access_token))
    assert ent.status_code == 200, ent.text
    body = ent.json()
    assert body["products"] == {"precheck": False, "secretaria": False}
    assert body["status"] == "inactive"
    assert body["plan"] == "free"


# --- Checkout session -----------------------------------------------------------------


async def _register(client, *, email: str, catalog_ids: list[str]) -> str:
    """Register a lead (first card) and return its intent_id."""
    resp = await client.post(
        "/public/signup-intents",
        json=_register_body(email=email, catalog_ids=catalog_ids),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["intent_id"]


#: Back-compat alias: other test modules (e.g. tests/test_billing_phase1.py) import this
#: helper to obtain a signup intent_id for a checkout test. It now registers a full lead
#: (the behavior change of this round), but still returns the intent_id the same way.
_create_intent = _register


async def test_checkout_session_unknown_intent_404(client):
    resp = await client.post(
        "/public/checkout-sessions",
        json={"intent_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert resp.status_code == 404


async def test_checkout_session_happy_path(client, monkeypatch):
    intent_id = await _register(
        client, email="checkout.happy@example.com", catalog_ids=["secretaria_basico"]
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
    # The signup checkout carries the INTENT id, never a tenant id (routing invariant).
    assert "metadata[tenant_id]" not in data


async def test_checkout_session_not_pending_conflict(client, monkeypatch):
    intent_id = await _register(
        client, email="checkout.conflict@example.com", catalog_ids=["secretaria_basico"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_signup_2", "url": "https://checkout.stripe.test/x"}
    )
    first = await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    assert first.status_code == 200, first.text

    # Activate it (webhook), which flips status away from pending_payment.
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
    intent_id = await _register(
        client, email="status.flow@example.com", catalog_ids=["secretaria_basico"]
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

    # The current token exchanges for a real session (the resume-in-another-browser path).
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
    intent_id = await _register(
        client, email="status.expired@example.com", catalog_ids=["secretaria_basico"]
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


# --- Webhook activation: inert -> active entitlement ----------------------------------


async def test_webhook_signup_intent_activates_entitlement_and_is_idempotent(client, monkeypatch):
    intent_id = await _register(
        client,
        email="provision.full@example.com",
        catalog_ids=["secretaria_basico", "multi_professional"],
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_provision_full", "url": "https://checkout.stripe.test/p"}
    )
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    # The tenant already EXISTS from registration (before any webhook), with an INERT
    # entitlement — this is the whole point of the register-at-first-card split.
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    matches = [t for t in tenants if t["clinic_name"] == "Clinica Signup Test"]
    assert len(matches) == 1
    tenant_id = matches[0]["id"]
    ent_before = (
        await client.get(f"/admin/tenants/{tenant_id}/entitlements", headers=_bearer(admin_token))
    ).json()
    assert ent_before["status"] == "inactive"
    assert ent_before["secretaria_enabled"] is False
    assert ent_before["precheck_enabled"] is False
    assert ent_before["plan"] == "free"

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

    # No SECOND tenant was created; the SAME row's entitlement is now ACTIVE.
    tenants_after = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()[
        "items"
    ]
    matches_after = [t for t in tenants_after if t["clinic_name"] == "Clinica Signup Test"]
    assert len(matches_after) == 1

    ent = (
        await client.get(f"/admin/tenants/{tenant_id}/entitlements", headers=_bearer(admin_token))
    ).json()
    assert ent["plan"] == "secretaria_basico"
    assert ent["secretaria_enabled"] is True
    assert ent["precheck_enabled"] is False
    assert ent["status"] == "active"
    assert ent["stripe_customer_id"] == "cus_provision_full"
    assert ent["stripe_subscription_id"] == "sub_provision_full"
    assert ent["addons"]["multi_professional"] is True
    # base (1) + one addon-active grant (1) = 2 — no quantity scaling on this path
    # (that only happens on the subscription.* events, not the signup-intent branch).
    assert ent["limits"]["professionals"] == 2
    # reminders is metering-only now (2026-07-22) -- no plan/add-on grants a base quota.
    assert ent["limits"]["reminders"] == 0

    # Idempotent: a redelivery of the SAME event is a no-op (still one tenant, still active).
    replay = await _post_webhook(
        client, _event("evt_provision_full", "checkout.session.completed", obj)
    )
    assert replay.status_code == 200
    assert replay.json() == {"received": True, "duplicate": True}

    ent_after_replay = (
        await client.get(f"/admin/tenants/{tenant_id}/entitlements", headers=_bearer(admin_token))
    ).json()
    assert ent_after_replay["status"] == "active"
    assert ent_after_replay["plan"] == "secretaria_basico"


# --- Exchange endpoint: unknown/garbage token ------------------------------------------


async def test_exchange_onboarding_token_unknown_rejected(client):
    resp = await client.post("/auth/exchange-onboarding-token", json={"token": "not-a-real-token"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_onboarding_token"


# --- Rate limiting ----------------------------------------------------------------------


async def test_signup_intent_rate_limited(client, monkeypatch):
    from brain_api.api import public_signup
    from brain_api.core.ratelimit import SlidingWindowLimiter

    monkeypatch.setattr(public_signup, "_limiter", SlidingWindowLimiter("t", lambda: 1))

    first = await client.post(
        "/public/signup-intents", json=_register_body(email="rate1@example.com")
    )
    assert first.status_code == 201
    second = await client.post(
        "/public/signup-intents", json=_register_body(email="rate2@example.com")
    )
    assert second.status_code == 429


# --- intake attach + onboarding-state seeding -----------------------------------------


async def test_attach_intake_endpoint_requires_doctor_token(client):
    """POST /doctor/onboarding/intake is gated by require_doctor (401 with no token)."""
    resp = await client.post(
        "/doctor/onboarding/intake",
        json={"whatsapp_usage": "business_recent", "prior_api": "no", "fb_page": "yes_admin"},
    )
    assert resp.status_code == 401


async def test_attach_intake_endpoint_stores_intake_on_intent(client):
    """The authenticated mid-wizard call attaches the intake to the caller's pending intent,
    which the webhook then reads to seed onboarding state."""
    reg = await client.post(
        "/public/signup-intents", json=_register_body(email="intake.http@example.com")
    )
    assert reg.status_code == 201, reg.text
    access_token = reg.json()["session"]["access_token"]

    attach = await client.post(
        "/doctor/onboarding/intake",
        headers=_bearer(access_token),
        json={"whatsapp_usage": "business_7d_plus", "prior_api": "yes", "fb_page": "no"},
    )
    assert attach.status_code == 204


async def test_attach_intake_rejects_invalid_literal(client):
    reg = await client.post(
        "/public/signup-intents", json=_register_body(email="intake.badhttp@example.com")
    )
    access_token = reg.json()["session"]["access_token"]
    resp = await client.post(
        "/doctor/onboarding/intake",
        headers=_bearer(access_token),
        json={"whatsapp_usage": "not_a_real_value", "prior_api": "no", "fb_page": "yes_admin"},
    )
    assert resp.status_code == 422


# --- Service-level: register -> attach -> activate wiring ------------------------------


async def test_register_signup_creates_tenant_user_and_inert_entitlement(db_session):
    from brain_api.models import Entitlement, User

    reg = await signup_service.register_signup(
        db_session,
        SignupIntentCreate(
            name="Dr. Lead",
            clinic_name="Wired Clinic",
            email="wired.reg@example.com",
            whatsapp_phone="+5511999990000",
            password=SIGNUP_PASSWORD,
            catalog_ids=["secretaria_basico"],
        ),
    )
    assert reg.intent.tenant_id is not None
    # Owner user created with a REAL password hash (verifiable), role tenant_owner.
    user = await db_session.get(User, reg.user.id)
    assert user is not None and user.role == "tenant_owner"
    from brain_api.core.security import verify_password

    assert verify_password(SIGNUP_PASSWORD, user.password_hash)
    # Inert entitlement: nothing purchased yet.
    ent = await db_session.get(Entitlement, reg.intent.tenant_id)
    assert ent is not None
    assert ent.status == "inactive"
    assert ent.secretaria_enabled is False
    assert ent.precheck_enabled is False


async def test_attach_intake_then_provision_seeds_onboarding_state(db_session):
    """register (no intake) -> attach_intake -> provision reads the attached intake and
    seeds the onboarding state (services.onboarding.provision_defaults)."""
    reg = await signup_service.register_signup(
        db_session,
        SignupIntentCreate(
            name="Dr. Lead",
            clinic_name="Wired Clinic",
            email="wired.intake@example.com",
            whatsapp_phone="+5511999990000",
            password=SIGNUP_PASSWORD,
            catalog_ids=["secretaria_basico"],
        ),
    )
    attached = await signup_service.attach_intake(
        db_session,
        reg.intent.tenant_id,
        IntakeIn(whatsapp_usage="business_recent", prior_api="yes", fb_page="no"),
    )
    assert attached is True

    await signup_service.provision_tenant_from_intent(
        db_session, reg.intent, stripe_customer_id="cus_wired", stripe_subscription_id="sub_wired"
    )

    tenant = await db_session.get(Tenant, reg.intent.tenant_id)
    assert tenant is not None
    # prior_api == "yes" takes priority over fb_page == "no" (derive_initial_state order).
    assert tenant.onboarding_state == onboarding.STATE_AGUARDANDO_ACAO_MANUAL
    assert tenant.blocker_reason == onboarding.BLOCKER_NUMERO_EM_OUTRO_BSP
    assert tenant.onboarding_anchor_at is not None
    assert tenant.config_reminder_anchor_at is not None
    # A manual-action blocker has no retry countdown yet.
    assert tenant.next_retry_at is None
    # And the entitlement is now active.
    assert reg.intent.status == "completed"


async def test_provision_none_intake_defaults_to_aquecimento(db_session):
    reg = await signup_service.register_signup(
        db_session,
        SignupIntentCreate(
            name="Dr. Lead2",
            clinic_name="Wired Clinic 2",
            email="wired.intake2@example.com",
            whatsapp_phone="+5511999990000",
            password=SIGNUP_PASSWORD,
            catalog_ids=["secretaria_basico"],
        ),
    )
    await signup_service.provision_tenant_from_intent(
        db_session, reg.intent, stripe_customer_id=None, stripe_subscription_id=None
    )

    tenant = await db_session.get(Tenant, reg.intent.tenant_id)
    assert tenant is not None
    assert tenant.onboarding_state == onboarding.STATE_AQUECIMENTO
    assert tenant.blocker_reason is None
    assert tenant.next_retry_at is not None  # aquecimento is retry-eligible


async def test_provision_missing_tenant_fails_gracefully(db_session):
    """Defensive: an intent whose linked tenant no longer exists (e.g. a pre-split intent)
    fails WITHOUT raising — the webhook must still ack."""
    from brain_api.models import SignupIntent

    intent = SignupIntent(
        name="Orphan",
        clinic_name="Orphan Clinic",
        email="orphan@example.com",
        whatsapp_phone="+5511999990000",
        catalog_ids=["secretaria_ferro"],
        tenant_id=None,  # never linked
    )
    db_session.add(intent)
    await db_session.flush()

    await signup_service.provision_tenant_from_intent(
        db_session, intent, stripe_customer_id="cus_x", stripe_subscription_id="sub_x"
    )
    assert intent.status == "failed"
    assert intent.failure_reason == "tenant_missing"


# --- Public checkout-funnel config -------------------------------------------------------


async def test_checkout_config_returns_configured_trial_period(client, monkeypatch):
    """GET /public/checkout-config echoes the REAL deployed STRIPE_TRIAL_PERIOD_DAYS —
    the funnel's pre-checkout disclosure copy quotes this instead of a hardcoded value."""
    from brain_api.api import public_signup

    monkeypatch.setattr(
        public_signup, "get_settings", lambda: SimpleNamespace(STRIPE_TRIAL_PERIOD_DAYS=75)
    )
    resp = await client.get("/public/checkout-config")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"trial_period_days": 75}


async def test_checkout_config_is_not_rate_limited(client, monkeypatch):
    """Unlike the three signup routes, this endpoint deliberately does NOT share the
    signup `_limiter` bucket — exhausting it must not affect checkout-config reads."""
    from brain_api.api import public_signup
    from brain_api.core.ratelimit import SlidingWindowLimiter

    monkeypatch.setattr(public_signup, "_limiter", SlidingWindowLimiter("t2", lambda: 0))
    resp = await client.get("/public/checkout-config")
    assert resp.status_code == 200, resp.text
