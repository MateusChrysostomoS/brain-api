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

import time
from types import SimpleNamespace
from uuid import UUID

from brain_api.models import Tenant
from brain_api.schemas.signup import IntakeIn, SignupIntentCreate
from brain_api.services import (
    billing as billing_service,
    catalog,
    onboarding,
    signup as signup_service,
)
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


def _install_setup_mode_stripe_fakes(
    monkeypatch,
    *,
    session_id: str = "cs_setup",
    subscription_id: str = "sub_setup",
    payment_method: str | None = "pm_saved_card",
    subscription_status: str = "trialing",
) -> list[dict]:
    """Fake `billing._stripe_get` / `billing._stripe_post` as plain async functions and
    return the recorded POST calls (in order).

    The setup-mode cold-signup flow makes THREE different Stripe calls across two requests
    — POST /v1/checkout/sessions (checkout creation), then GET /v1/setup_intents/{id} +
    POST /v1/subscriptions (webhook) — which `_install_fake_stripe_httpx`'s single fixed
    `response_body` cannot express. Same monkeypatch-the-helpers pattern
    tests/test_test_window.py uses for the restart endpoint's multi-call branch.

    The `POST /v1/subscriptions` reply mirrors the REAL Stripe response shape: a
    subscription created with `trial_period_days` comes back `status: "trialing"` (verified
    against the live test API), and `provision_tenant_from_intent` now activates the
    entitlement with exactly that status instead of hardcoding "active".
    """
    calls: list[dict] = []
    now = int(time.time())

    async def fake_stripe_get(path: str) -> dict:
        if path.startswith("/v1/setup_intents/"):
            return {"id": path.rsplit("/", 1)[-1], "payment_method": payment_method}
        raise AssertionError(f"unexpected _stripe_get path: {path}")

    async def fake_stripe_post(
        path: str, data: dict, *, idempotency_key: str | None = None
    ) -> dict:
        calls.append({"path": path, "data": data, "idempotency_key": idempotency_key})
        if path == "/v1/checkout/sessions":
            return {"id": session_id, "url": f"https://checkout.stripe.test/{session_id}"}
        if path == "/v1/subscriptions":
            return {
                "id": subscription_id,
                "status": subscription_status,
                "current_period_start": now,
                "current_period_end": now + 30 * 24 * 3600,
            }
        raise AssertionError(f"unexpected _stripe_post path: {path}")

    monkeypatch.setattr(billing_service, "_stripe_get", fake_stripe_get)
    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_post)
    return calls


async def _intent_row(intent_id: str):
    """Re-read a `SignupIntent` straight from the `client` fixture's DB (a fresh session
    over the SAME engine, via the app's own get_session override — the pattern
    tests/test_smoke.py uses). Needed because `status`/`failure_reason` are deliberately
    NOT exposed on any authenticated route."""
    from brain_api.core.database import get_session
    from brain_api.main import app
    from brain_api.models import SignupIntent

    gen = app.dependency_overrides[get_session]()
    session = await gen.__anext__()
    try:
        return await session.get(SignupIntent, UUID(intent_id))
    finally:
        await gen.aclose()


async def _force_intent_pending(intent_id: str) -> None:
    """Rewind an intent to `pending_payment` in the DB, simulating the ONE case a Stripe
    redelivery genuinely re-runs the whole handler: a crash between the Stripe POST and
    our commit, which rolls the `processed_stripe_events` dedup row AND the intent's
    status transition back together."""
    from brain_api.core.database import get_session
    from brain_api.main import app
    from brain_api.models import SignupIntent

    gen = app.dependency_overrides[get_session]()
    session = await gen.__anext__()
    try:
        intent = await session.get(SignupIntent, UUID(intent_id))
        intent.status = "pending_payment"
        await session.commit()
    finally:
        await gen.aclose()


def _setup_completed_obj(intent_id: str, *, customer: str, setup_intent: str) -> dict:
    """A REAL `mode=setup` `checkout.session.completed` object: it carries `setup_intent`
    and NO `subscription` (the subscription is ours to create, server-side)."""
    return {
        "customer": customer,
        "setup_intent": setup_intent,
        "client_reference_id": intent_id,
        "metadata": {"kind": "signup_intent", "signup_intent_id": intent_id},
    }


async def test_checkout_session_unknown_intent_404(client):
    resp = await client.post(
        "/public/checkout-sessions",
        json={"intent_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert resp.status_code == 404


async def test_checkout_session_happy_path(client, monkeypatch):
    """The signup Checkout Session is SETUP mode: it saves a card and nothing else, so
    Stripe renders none of its own billing/trial wording. No `line_items`, no
    `subscription_data` (Stripe rejects both in setup mode) — the subscription, with the
    trial, is created server-side from the webhook instead."""
    intent_id = await _register(
        client, email="checkout.happy@example.com", catalog_ids=["secretaria_basico"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch,
        captured,
        {"id": "cs_signup_1", "url": "https://checkout.stripe.test/signup"},
        trial_period_days=30,
    )

    resp = await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"checkout_url": "https://checkout.stripe.test/signup"}

    data = captured["data"]
    assert data["mode"] == "setup"
    assert not [key for key in data if key.startswith("line_items[")]
    assert not [key for key in data if key.startswith("subscription_data[")]
    # The one piece of billing wording on the page is ours (setup mode also supports it).
    assert data["custom_text[submit][message]"]
    assert data["metadata[kind]"] == "signup_intent"
    assert data["metadata[signup_intent_id]"] == intent_id
    assert data["client_reference_id"] == intent_id
    assert data["customer_email"] == "checkout.happy@example.com"
    # Setup mode has no line items to infer a currency from, so Stripe REQUIRES one
    # explicitly (it 400s the whole request without it) — verified against the live API.
    assert data["currency"] == "brl"
    # Conversely `phone_number_collection` is valid ONLY in payment/subscription mode and
    # 400s here — the buyer's number already rides SignupIntent.whatsapp_phone anyway.
    assert "phone_number_collection[enabled]" not in data
    # The signup checkout carries the INTENT id, never a tenant id (routing invariant).
    assert "metadata[tenant_id]" not in data
    # THE blocker fix: Stripe does NOT create a Customer for a setup-mode session on its
    # own (`customer_creation` defaults to "if_required", and `customer_email` does not
    # make one required — a completed session came back `customer: null` against the live
    # API). Without a Customer there is nothing to subscribe, so every signup would end up
    # entitled and never billed.
    assert data["customer_creation"] == "always"


async def test_checkout_session_no_trial_configured_has_no_custom_text(client, monkeypatch):
    """`_apply_setup_custom_text` mirrors `_apply_trial`'s gate: with
    STRIPE_TRIAL_PERIOD_DAYS at 0/off there is no test window to describe, so no invented
    messaging about one."""
    intent_id = await _register(
        client, email="checkout.notrial@example.com", catalog_ids=["secretaria_basico"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_signup_nt", "url": "https://checkout.stripe.test/nt"}
    )
    resp = await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    assert resp.status_code == 200, resp.text
    assert captured["data"]["mode"] == "setup"
    assert "custom_text[submit][message]" not in captured["data"]


async def test_checkout_session_not_pending_conflict(client, monkeypatch):
    intent_id = await _register(
        client, email="checkout.conflict@example.com", catalog_ids=["secretaria_basico"]
    )
    calls = _install_setup_mode_stripe_fakes(
        monkeypatch, session_id="cs_signup_2", subscription_id="sub_conflict"
    )
    first = await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    assert first.status_code == 200, first.text

    # Activate it (webhook), which flips status away from pending_payment. A real
    # setup-mode event carries `setup_intent`, never `subscription`.
    webhook_resp = await _post_webhook(
        client,
        _event(
            "evt_signup_conflict",
            "checkout.session.completed",
            _setup_completed_obj(intent_id, customer="cus_conflict", setup_intent="seti_conflict"),
        ),
    )
    assert webhook_resp.status_code == 200
    assert [c["path"] for c in calls] == ["/v1/checkout/sessions", "/v1/subscriptions"]

    second = await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    assert second.status_code == 409


async def test_setup_webhook_creates_subscription_and_activates(client, monkeypatch):
    """End-to-end of the new flow: a `mode=setup` completion resolves the saved card off
    the SetupIntent, creates the subscription server-side with it, and hands the resulting
    id to `provision_tenant_from_intent` (which links it on the entitlement)."""
    intent_id = await _register(
        client, email="setup.creates@example.com", catalog_ids=["secretaria_basico"]
    )
    calls = _install_setup_mode_stripe_fakes(
        monkeypatch, session_id="cs_setup_creates", subscription_id="sub_setup_created"
    )
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200

    resp = await _post_webhook(
        client,
        _event(
            "evt_setup_creates",
            "checkout.session.completed",
            _setup_completed_obj(intent_id, customer="cus_setup", setup_intent="seti_setup"),
        ),
    )
    assert resp.status_code == 200, resp.text

    sub_calls = [c for c in calls if c["path"] == "/v1/subscriptions"]
    assert len(sub_calls) == 1
    data = sub_calls[0]["data"]
    assert data["customer"] == "cus_setup"
    assert data["default_payment_method"] == "pm_saved_card"
    assert data["metadata[signup_intent_id]"] == intent_id
    # `items[i]`, NOT `line_items[i]` — this is POST /v1/subscriptions, not Checkout.
    assert data["items[0][price]"] == "price_ferro"
    assert not [key for key in data if key.startswith("line_items[")]

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    tenant_id = [t for t in tenants if t["clinic_name"] == "Clinica Signup Test"][0]["id"]
    # The subscription carries the TENANT id too, so a later `customer.subscription.*`
    # event resolves through `_entitlement_for_event`'s FIRST branch (metadata) instead of
    # racing the `stripe_customer_id` column this same handler is still writing.
    assert data["metadata[tenant_id]"] == tenant_id
    ent = (
        await client.get(f"/admin/tenants/{tenant_id}/entitlements", headers=_bearer(admin_token))
    ).json()
    # NOT "active": the subscription we just created is TRIALING, and both test-window
    # mechanisms (harden_charge, trial_will_end auto-cancel) gate on exactly that status.
    assert ent["status"] == "trialing"
    assert ent["stripe_customer_id"] == "cus_setup"
    assert ent["stripe_subscription_id"] == "sub_setup_created"


async def test_setup_webhook_entitlement_status_follows_subscription_status(client, monkeypatch):
    """Money-correctness: the entitlement status is whatever the subscription Stripe just
    created reports — never a hardcoded "active".

    A hardcoded "active" silently disables BOTH halves of the Meta/WABA test window:
    `harden_charge` returns early unless the row says `trialing` (so a tenant that connects
    WhatsApp is never billed on its normal cadence), and the `trial_will_end` handler
    requires it too (so the auto-cancel is never scheduled and a tenant Meta never approved
    DOES get charged — the exact opposite of what the checkout page promises).
    """
    intent_id = await _register(
        client, email="setup.trialing@example.com", catalog_ids=["secretaria_basico"]
    )
    _install_setup_mode_stripe_fakes(
        monkeypatch,
        session_id="cs_setup_trialing",
        subscription_id="sub_setup_trialing",
        subscription_status="trialing",
    )
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200
    assert (
        await _post_webhook(
            client,
            _event(
                "evt_setup_trialing",
                "checkout.session.completed",
                _setup_completed_obj(
                    intent_id, customer="cus_trialing", setup_intent="seti_trialing"
                ),
            ),
        )
    ).status_code == 200

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    tenant_id = [t for t in tenants if t["clinic_name"] == "Clinica Signup Test"][0]["id"]
    ent = (
        await client.get(f"/admin/tenants/{tenant_id}/entitlements", headers=_bearer(admin_token))
    ).json()
    assert ent["status"] == "trialing"
    # The period the subscription reported is carried over too (it used to stay null until
    # a later customer.subscription.* event happened to resolve).
    assert ent["period_start"] is not None
    assert ent["period_end"] is not None
    # Products/plan still come from the intent's catalog_ids, unchanged by this.
    assert ent["plan"] == "secretaria_basico"
    assert ent["secretaria_enabled"] is True


async def test_setup_webhook_without_payment_method_creates_no_subscription(client, monkeypatch):
    """A SetupIntent with no `payment_method` (still `processing`) must NOT produce a
    card-less subscription — that looks healthy for the whole trial and then cannot be
    charged. It fails LOUD so Stripe redelivers once the SetupIntent has settled."""
    intent_id = await _register(
        client, email="setup.nopm@example.com", catalog_ids=["secretaria_basico"]
    )
    calls = _install_setup_mode_stripe_fakes(
        monkeypatch, session_id="cs_setup_nopm", payment_method=None
    )
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200

    resp = await _post_webhook(
        client,
        _event(
            "evt_setup_nopm",
            "checkout.session.completed",
            _setup_completed_obj(intent_id, customer="cus_nopm", setup_intent="seti_nopm"),
        ),
    )
    # Non-2xx => Stripe redelivers (the whole point); no subscription was created.
    assert resp.status_code >= 500
    assert not [c for c in calls if c["path"] == "/v1/subscriptions"]

    # Nothing was committed: the intent is still pending, so the redelivery re-runs it.
    intent = await _intent_row(intent_id)
    assert intent.status == "pending_payment"


async def test_setup_webhook_subscription_create_is_idempotent_on_redelivery(client, monkeypatch):
    """A re-run of this handler for the SAME signup intent can never mint a SECOND
    subscription.

    THREE guards, in the order they actually fire:
    1. A literal Stripe redelivery (same `event.id`) short-circuits on the
       `processed_stripe_events` dedup row BEFORE any handler code runs — zero extra
       Stripe calls.
    2. A redelivery that gets PAST guard 1 (a fresh event id — e.g. the second of two
       checkout sessions for one intent) finds `intent.status == "completed"` and reuses
       `intent.stripe_subscription_id` instead of POSTing anything. This is the guard that
       matters most: Stripe expires idempotency keys after ~24h but keeps redelivering a
       failing webhook for ~3 days, so a day-2 redelivery relying on the key alone would
       create a SECOND, orphaned, double-billing subscription.
    3. Only when the handler genuinely re-runs on a still-`pending_payment` intent — the
       crash-between-the-Stripe-POST-and-our-commit case, which rolls the dedup row AND
       the status transition back together — does a second POST happen, and then it
       carries the IDENTICAL deterministic `Idempotency-Key` so Stripe returns the
       original subscription rather than creating another.
    """
    intent_id = await _register(
        client, email="setup.idempotent@example.com", catalog_ids=["secretaria_basico"]
    )
    calls = _install_setup_mode_stripe_fakes(
        monkeypatch, session_id="cs_setup_idem", subscription_id="sub_setup_idem"
    )
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200

    obj = _setup_completed_obj(intent_id, customer="cus_idem", setup_intent="seti_idem")
    event = _event("evt_setup_idem", "checkout.session.completed", obj)

    first = await _post_webhook(client, event)
    assert first.json() == {"received": True, "duplicate": False}
    sub_calls = [c for c in calls if c["path"] == "/v1/subscriptions"]
    assert len(sub_calls) == 1

    # Guard 1: identical event id -> deduped, no second Stripe call at all.
    replay = await _post_webhook(client, event)
    assert replay.json() == {"received": True, "duplicate": True}
    assert len([c for c in calls if c["path"] == "/v1/subscriptions"]) == 1

    # Guard 2: fresh event id, so the handler DOES run — but the intent is already
    # `completed`, so it reuses the stored subscription id and never touches Stripe.
    rerun = await _post_webhook(
        client, _event("evt_setup_idem_2", "checkout.session.completed", obj)
    )
    assert rerun.json() == {"received": True, "duplicate": False}
    assert len([c for c in calls if c["path"] == "/v1/subscriptions"]) == 1

    # Guard 3: rewind the intent to pending (what a crash-rolled-back transaction leaves
    # behind) — NOW the handler really re-creates, with the SAME deterministic key.
    await _force_intent_pending(intent_id)
    crash_rerun = await _post_webhook(
        client, _event("evt_setup_idem_3", "checkout.session.completed", obj)
    )
    assert crash_rerun.json() == {"received": True, "duplicate": False}
    sub_calls = [c for c in calls if c["path"] == "/v1/subscriptions"]
    assert len(sub_calls) == 2
    assert sub_calls[0]["idempotency_key"] == f"signup-sub-{intent_id}"
    assert sub_calls[1]["idempotency_key"] == sub_calls[0]["idempotency_key"]


async def test_setup_webhook_already_completed_intent_never_reposts(client, monkeypatch):
    """The `already_completed` short-circuit, isolated: a redelivery with a fresh event id
    for an intent that was already activated makes ZERO Stripe calls and leaves the linked
    subscription id untouched (it is read back off the intent, not re-created)."""
    intent_id = await _register(
        client, email="setup.completed@example.com", catalog_ids=["secretaria_basico"]
    )
    calls = _install_setup_mode_stripe_fakes(
        monkeypatch, session_id="cs_setup_done", subscription_id="sub_setup_done"
    )
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200
    obj = _setup_completed_obj(intent_id, customer="cus_done", setup_intent="seti_done")
    assert (
        await _post_webhook(client, _event("evt_done_1", "checkout.session.completed", obj))
    ).status_code == 200

    calls.clear()
    assert (
        await _post_webhook(client, _event("evt_done_2", "checkout.session.completed", obj))
    ).status_code == 200
    assert calls == []

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    tenant_id = [t for t in tenants if t["clinic_name"] == "Clinica Signup Test"][0]["id"]
    ent = (
        await client.get(f"/admin/tenants/{tenant_id}/entitlements", headers=_bearer(admin_token))
    ).json()
    assert ent["stripe_subscription_id"] == "sub_setup_done"
    assert ent["status"] == "trialing"


async def test_setup_webhook_without_customer_fails_intent_and_grants_nothing(
    client, monkeypatch
):
    """A setup completion with no `customer` cannot be subscribed (Stripe requires one).
    With `customer_creation=always` this is unreachable, so if it ever fires it is a
    genuine anomaly — and it must NOT hand out a free entitlement.

    The old behavior "degraded" into `provision_tenant_from_intent(..., None, None)`, which
    activated the tenant in full (`status="active"`, `secretaria_enabled=True`) with no
    subscription and no customer: a working product, permanently unbilled, with the webhook
    still answering 200 and nothing anywhere recording it. Now the intent is marked
    `failed` (the same convention as `tenant_missing`), nothing is activated, and the buyer
    sees `failed` on the onboarding poll rather than a spinner that never resolves.
    """
    intent_id = await _register(
        client, email="setup.nocustomer@example.com", catalog_ids=["secretaria_basico"]
    )
    calls = _install_setup_mode_stripe_fakes(monkeypatch, session_id="cs_setup_nocus")
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200

    obj = {
        "setup_intent": "seti_orphan",
        "metadata": {"kind": "signup_intent", "signup_intent_id": intent_id},
    }
    resp = await _post_webhook(
        client, _event("evt_setup_nocus", "checkout.session.completed", obj)
    )
    # Still acks (redelivering cannot fix a missing customer), but creates nothing.
    assert resp.status_code == 200, resp.text
    assert not [c for c in calls if c["path"] == "/v1/subscriptions"]

    intent = await _intent_row(intent_id)
    assert intent.status == "failed"
    assert intent.failure_reason == "stripe_customer_missing"

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    tenant_id = [t for t in tenants if t["clinic_name"] == "Clinica Signup Test"][0]["id"]
    ent = (
        await client.get(f"/admin/tenants/{tenant_id}/entitlements", headers=_bearer(admin_token))
    ).json()
    # The entitlement stays INERT — no free product.
    assert ent["status"] == "inactive"
    assert ent["plan"] == "free"
    assert ent["secretaria_enabled"] is False
    assert ent["stripe_subscription_id"] is None

    # And the buyer's poll surfaces the failure instead of spinning forever.
    status_resp = await client.get(
        "/public/onboarding-status", params={"session_id": "cs_setup_nocus"}
    )
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()["status"] == "failed"


async def test_setup_webhook_unpriced_plan_fails_intent_and_acks(client, monkeypatch):
    """A PERMANENT derive/create failure (here: `STRIPE_PRICE_MAP` no longer prices the
    purchased plan, e.g. it was edited between checkout and webhook) marks the intent
    failed and ACKS — three days of redeliveries cannot fix a config problem, and the
    500-with-no-intent-id it used to produce named nothing."""
    intent_id = await _register(
        client, email="setup.unpriced@example.com", catalog_ids=["secretaria_basico"]
    )
    calls = _install_setup_mode_stripe_fakes(monkeypatch, session_id="cs_setup_unpriced")
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200

    # Pull the plan's price (and all three metered companions) out from under the webhook.
    monkeypatch.setattr(billing_service, "price_id_for", lambda catalog_id: None)

    resp = await _post_webhook(
        client,
        _event(
            "evt_setup_unpriced",
            "checkout.session.completed",
            _setup_completed_obj(intent_id, customer="cus_unpriced", setup_intent="seti_unpriced"),
        ),
    )
    assert resp.status_code == 200, resp.text
    assert not [c for c in calls if c["path"] == "/v1/subscriptions"]

    intent = await _intent_row(intent_id)
    assert intent.status == "failed"
    assert intent.failure_reason == "subscription_create_failed"

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    tenant_id = [t for t in tenants if t["clinic_name"] == "Clinica Signup Test"][0]["id"]
    ent = (
        await client.get(f"/admin/tenants/{tenant_id}/entitlements", headers=_bearer(admin_token))
    ).json()
    assert ent["status"] == "inactive"
    assert ent["secretaria_enabled"] is False


# --- PATCH /public/signup-intents/{id} — add-on selection update (corrections round) --
#
# The test STRIPE_PRICE_MAP (conftest.py) configures prices for: complete_clinic_combo,
# secretaria_basico, precheck (plans) + multi_professional, reactivation_pack, ehr
# (add-ons). verified_identity/multi_unit/pix_deposit/analytics_bi/
# analytics_bi_advanced/human_backup_24_7 are deliberately UNPRICED there, for the
# addon_not_available case below.


async def test_patch_intent_adds_addon(client):
    intent_id = await _register(
        client, email="patch.add@example.com", catalog_ids=["secretaria_basico"]
    )
    resp = await client.patch(
        f"/public/signup-intents/{intent_id}",
        json={"catalog_ids": ["secretaria_basico", "ehr"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "intent_id": intent_id,
        "catalog_ids": ["secretaria_basico", "ehr"],
        "status": "pending_payment",
    }


async def test_patch_intent_removes_addon(client):
    intent_id = await _register(
        client,
        email="patch.remove@example.com",
        catalog_ids=["secretaria_basico", "multi_professional"],
    )
    resp = await client.patch(
        f"/public/signup-intents/{intent_id}", json={"catalog_ids": ["secretaria_basico"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["catalog_ids"] == ["secretaria_basico"]


async def test_patch_intent_normalizes_plan_first_and_dedupes(client):
    """Persisted order is normalized (plan first, add-ons deduped) regardless of what
    order/duplication the client sent — documented `update_intent_catalog` behavior."""
    intent_id = await _register(
        client, email="patch.normalize@example.com", catalog_ids=["secretaria_basico"]
    )
    resp = await client.patch(
        f"/public/signup-intents/{intent_id}",
        json={"catalog_ids": ["multi_professional", "secretaria_basico", "multi_professional"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["catalog_ids"] == ["secretaria_basico", "multi_professional"]


async def test_patch_intent_unknown_id_404(client):
    resp = await client.patch(
        "/public/signup-intents/00000000-0000-0000-0000-000000000000",
        json={"catalog_ids": ["secretaria_basico"]},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "signup_intent_not_found"


async def test_patch_intent_not_pending_conflict(client, monkeypatch):
    intent_id = await _register(
        client, email="patch.notpending@example.com", catalog_ids=["secretaria_basico"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_patch_np", "url": "https://checkout.stripe.test/np"}
    )
    assert (
        await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    ).status_code == 200
    obj = {
        "customer": "cus_patch_np",
        "subscription": "sub_patch_np",
        "metadata": {"kind": "signup_intent", "signup_intent_id": intent_id},
    }
    assert (
        await _post_webhook(client, _event("evt_patch_np", "checkout.session.completed", obj))
    ).status_code == 200

    resp = await client.patch(
        f"/public/signup-intents/{intent_id}", json={"catalog_ids": ["secretaria_basico", "ehr"]}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "intent_not_pending"


async def test_patch_intent_plan_change_not_allowed(client):
    """Target plan must be a REAL, currently-assignable catalog plan for this to exercise
    `plan_change_not_allowed` (rather than 422 unknown_catalog_ids) -- "precheck_basic",
    not the legacy bare "precheck" alias, which `_validate_catalog_ids` no longer
    recognizes as a plan id since the 2026-08-01 PreCheck-billing split."""
    intent_id = await _register(
        client, email="patch.planchange@example.com", catalog_ids=["secretaria_basico"]
    )
    resp = await client.patch(
        f"/public/signup-intents/{intent_id}", json={"catalog_ids": ["precheck_basic"]}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "plan_change_not_allowed"


async def test_patch_intent_unknown_catalog_id_422(client):
    intent_id = await _register(
        client, email="patch.unknownid@example.com", catalog_ids=["secretaria_basico"]
    )
    resp = await client.patch(
        f"/public/signup-intents/{intent_id}",
        json={"catalog_ids": ["secretaria_basico", "not_a_real_catalog_id"]},
    )
    assert resp.status_code == 422


async def test_patch_intent_addon_not_available_409(client):
    """Defense-in-depth: an add-on with no configured Stripe price is rejected even
    though the frontend is only supposed to ever offer add-ons `GET
    /public/checkout-config` reports as available."""
    intent_id = await _register(
        client, email="patch.unavailable@example.com", catalog_ids=["secretaria_basico"]
    )
    resp = await client.patch(
        f"/public/signup-intents/{intent_id}",
        json={"catalog_ids": ["secretaria_basico", "verified_identity"]},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "addon_not_available"


async def test_patch_intent_shares_signup_rate_limit_bucket(client, monkeypatch):
    """The PATCH route participates in the SAME shared `_limiter` bucket as the other
    public signup routes (not a separate budget): exhausting the (fresh, 1-per-window)
    bucket via a DIFFERENT route still 429s the PATCH call right after."""
    from brain_api.api import public_signup
    from brain_api.core.ratelimit import SlidingWindowLimiter

    intent_id = await _register(
        client, email="patch.ratelimit@example.com", catalog_ids=["secretaria_basico"]
    )
    monkeypatch.setattr(public_signup, "_limiter", SlidingWindowLimiter("t3", lambda: 1))

    # Consume the ONE allowed hit via a different shared-bucket route.
    first = await client.get("/public/onboarding-status", params={"session_id": "whatever"})
    assert first.status_code == 404  # unknown session, but it got PAST the limiter

    resp = await client.patch(
        f"/public/signup-intents/{intent_id}", json={"catalog_ids": ["secretaria_basico"]}
    )
    assert resp.status_code == 429


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
    # Owner user created with a REAL password hash (verifiable), role doctor +
    # is_owner/is_manager both true (role-taxonomy round).
    user = await db_session.get(User, reg.user.id)
    assert user is not None and user.role == "doctor"
    assert user.is_owner is True
    assert user.is_manager is True
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


# --- Webhook: the secretaria-provisioning bridge is gated on secretaria_enabled -------
#
# precheck-billing round fix: services.billing.apply_stripe_event's signup-intent branch
# used to ping onboarding_sync.ensure_secretaria_provisioned unconditionally after ANY
# activation. A PreCheck-only signup has no WhatsApp/secretaria component at all and must
# never trigger it. Exercised directly against `billing_service.apply_stripe_event` (a
# `subscription` id riding the event object takes the "legacy subscription-mode" branch
# inside `_apply_signup_intent_checkout`, so no Stripe call happens at all here) rather
# than through the full checkout HTTP round-trip, mirroring tests/test_billing_phase1.py's
# direct-service-layer style for webhook internals.


async def test_precheck_only_signup_does_not_provision_secretaria(db_session, monkeypatch):
    from brain_api.models import Entitlement
    from brain_api.services import onboarding_sync

    calls: list = []

    async def fake_ensure(session, tenant):
        calls.append(tenant.id)

    monkeypatch.setattr(onboarding_sync, "ensure_secretaria_provisioned", fake_ensure)

    reg = await signup_service.register_signup(
        db_session,
        SignupIntentCreate(
            name="Dr. Precheck",
            clinic_name="Precheck Only Gate Clinic",
            email="precheck.only.gate@example.com",
            whatsapp_phone="+5511999990000",
            password=SIGNUP_PASSWORD,
            catalog_ids=["precheck_basic"],
        ),
    )

    applied = await billing_service.apply_stripe_event(
        db_session,
        "evt_precheck_only_gate",
        "checkout.session.completed",
        {
            "customer": "cus_precheck_only_gate",
            "subscription": "sub_precheck_only_gate",
            "metadata": {"kind": "signup_intent", "signup_intent_id": str(reg.intent.id)},
        },
    )
    assert applied is True
    assert reg.intent.status == "completed"

    ent = await db_session.get(Entitlement, reg.intent.tenant_id)
    assert ent.precheck_enabled is True
    assert ent.secretaria_enabled is False

    assert calls == []  # never pinged


async def test_secretaria_signup_still_provisions_secretaria(db_session, monkeypatch):
    """Contrast case: a secretarIA-enabling signup still fires the provisioning bridge —
    proves the fix is a GATE, not an accidental blanket removal of the call."""
    from brain_api.models import Entitlement
    from brain_api.services import onboarding_sync

    calls: list = []

    async def fake_ensure(session, tenant):
        calls.append(tenant.id)

    monkeypatch.setattr(onboarding_sync, "ensure_secretaria_provisioned", fake_ensure)

    reg = await signup_service.register_signup(
        db_session,
        SignupIntentCreate(
            name="Dr. Secretaria",
            clinic_name="Secretaria Gate Clinic",
            email="secretaria.gate@example.com",
            whatsapp_phone="+5511999990000",
            password=SIGNUP_PASSWORD,
            catalog_ids=["secretaria_basico"],
        ),
    )

    applied = await billing_service.apply_stripe_event(
        db_session,
        "evt_secretaria_gate",
        "checkout.session.completed",
        {
            "customer": "cus_secretaria_gate",
            "subscription": "sub_secretaria_gate",
            "metadata": {"kind": "signup_intent", "signup_intent_id": str(reg.intent.id)},
        },
    )
    assert applied is True

    ent = await db_session.get(Entitlement, reg.intent.tenant_id)
    assert ent.secretaria_enabled is True

    assert calls == [reg.intent.tenant_id]


# --- Public checkout-funnel config -------------------------------------------------------


async def test_checkout_config_returns_configured_trial_period(client, monkeypatch):
    """GET /public/checkout-config echoes the REAL deployed STRIPE_TRIAL_PERIOD_DAYS —
    the funnel's pre-checkout disclosure copy quotes this instead of a hardcoded value.

    The monkeypatch only replaces `public_signup.get_settings` (what `trial_period_days`
    reads); `addons` is computed via `billing.price_id_for`'s OWN `get_settings()` call
    and is unaffected — see test_checkout_config_addons_reflect_price_map_availability
    for its full shape.
    """
    from brain_api.api import public_signup

    monkeypatch.setattr(
        public_signup, "get_settings", lambda: SimpleNamespace(STRIPE_TRIAL_PERIOD_DAYS=75)
    )
    resp = await client.get("/public/checkout-config")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trial_period_days"] == 75
    assert len(body["addons"]) == len(catalog.ADDON_IDS)


async def test_checkout_config_addons_reflect_price_map_availability(client):
    """`addons` covers every `catalog.ADDON_IDS` id in stable alphabetical order;
    `available=True` exactly for the ids the test STRIPE_PRICE_MAP (conftest.py) prices:
    multi_professional, reactivation_pack, ehr."""
    resp = await client.get("/public/checkout-config")
    assert resp.status_code == 200, resp.text
    addons = resp.json()["addons"]
    assert [a["id"] for a in addons] == sorted(catalog.ADDON_IDS)
    available = {a["id"] for a in addons if a["available"]}
    assert available == {"multi_professional", "reactivation_pack", "ehr"}
    unavailable = {a["id"] for a in addons if not a["available"]}
    assert unavailable == set(catalog.ADDON_IDS) - available


async def test_checkout_config_is_not_rate_limited(client, monkeypatch):
    """Unlike the three signup routes, this endpoint deliberately does NOT share the
    signup `_limiter` bucket — exhausting it must not affect checkout-config reads."""
    from brain_api.api import public_signup
    from brain_api.core.ratelimit import SlidingWindowLimiter

    monkeypatch.setattr(public_signup, "_limiter", SlidingWindowLimiter("t2", lambda: 0))
    resp = await client.get("/public/checkout-config")
    assert resp.status_code == 200, resp.text
