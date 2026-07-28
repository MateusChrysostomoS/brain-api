"""Task 2 — reframe the free trial as a Meta/WABA acceptance test-window (backend).

Ground truth: services/billing.py (`harden_charge`, `_reset_markers_if_subscription_changed`,
`_restart_test_window`, `_append_subscription_items`), services/onboarding_sync.py
(`test_window_email_due`, `apply_onboarding_event`), services/signup.py
(`provision_tenant_from_intent`), api/onboarding.py (the 'conectado' harden wiring in
`post_attempt` + the new `GET`/`POST /doctor/onboarding/test-window*` routes), api/internal.py
(the three new `InternalOnboardingTenantOut` fields). Reuses the Stripe test doubles from
tests/test_billing.py (real HMAC-signed webhook helper, `_admin_entitlements`), the seeded
client fixture + tenants from tests/test_rbac.py, and tests/test_signup.py's registration
helper.
"""

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from brain_api.models import Entitlement, Tenant
from brain_api.services import (
    billing as billing_service,
    onboarding,
    onboarding_sync,
    secretaria_provisioning,
)
from tests.test_billing import (
    _admin_entitlements,
    _event,
    _install_fake_stripe_httpx,
    _post_webhook,
)
from tests.test_onboarding_endpoints import _set_pair_key
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
from tests.test_signup import SIGNUP_PASSWORD, _create_intent


def _noop_async(value):
    async def _f(*args, **kwargs):
        return value

    return _f


async def _tenant_ids(client, admin_token: str) -> dict[str, str]:
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    return {t["clinic_name"]: t["id"] for t in tenants}


async def _link_and_set_plan(
    client, tenant_id: str, *, customer_id: str, subscription_id: str, status: str = "trialing"
) -> None:
    """Link a Stripe customer+subscription to `tenant_id` and set its plan to
    secretaria_basico (two real webhook deliveries, mirrors tests/test_billing.py)."""
    await _post_webhook(
        client,
        _event(
            f"evt_link_{subscription_id}",
            "checkout.session.completed",
            {
                "customer": customer_id,
                "subscription": subscription_id,
                "metadata": {"tenant_id": tenant_id},
            },
        ),
    )
    now_ts = int(time.time())
    resp = await _post_webhook(
        client,
        _event(
            f"evt_sub_{subscription_id}",
            "customer.subscription.created",
            {
                "id": subscription_id,
                "customer": customer_id,
                "status": status,
                "current_period_start": now_ts,
                "current_period_end": now_ts + 30 * 86400,
                "items": {"data": [{"price": {"id": "price_ferro"}, "quantity": 1}]},
                "metadata": {"tenant_id": tenant_id},
            },
        ),
    )
    assert resp.status_code == 200, resp.text


# ===========================================================================================
# (a) POST /doctor/onboarding/attempts -> 'conectado' triggers harden_charge
# ===========================================================================================


async def test_attempt_pass_at_conectado_triggers_harden_charge(client, monkeypatch):
    """Task 2's core billing-semantics change: the billing cycle anchors at 'conectado'
    (Meta acceptance), not at the LATER 'ativo' transition."""
    monkeypatch.setattr(
        secretaria_provisioning,
        "connect_whatsapp",
        _noop_async(secretaria_provisioning.CONNECTION_OK),
    )
    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _noop_async(True))
    monkeypatch.setattr(secretaria_provisioning, "get_config_status", _noop_async(None))

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    await _link_and_set_plan(
        client,
        tenant_a_id,
        customer_id="cus_harden_conectado",
        subscription_id="sub_harden_conectado",
    )

    captured: dict = {}

    async def fake_stripe_post(path, data):
        captured["path"] = path
        captured["data"] = data
        return {}

    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_post)

    owner_a_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/doctor/onboarding/attempts",
        headers=_bearer(owner_a_token),
        json={"attempt_id": str(uuid4()), "result": "pass", "phone_number_id": "5511999990000"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["onboarding_state"] == "conectado"

    assert captured["path"] == "/v1/subscriptions/sub_harden_conectado"
    assert captured["data"] == {
        "trial_end": "now",
        "proration_behavior": "none",
        "cancel_at": "",
    }
    ent = await _admin_entitlements(client, tenant_a_id)
    assert ent["status"] == "trialing"  # harden_charge does not itself flip status locally


async def test_attempt_pass_at_conectado_noop_when_not_trialing(client, monkeypatch):
    """The pre-existing tenant A fixture (plan=precheck, status=active, no subscription)
    must NOT attempt a Stripe call -- harden_charge's own guards short-circuit quietly."""
    monkeypatch.setattr(
        secretaria_provisioning,
        "connect_whatsapp",
        _noop_async(secretaria_provisioning.CONNECTION_OK),
    )
    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _noop_async(True))
    monkeypatch.setattr(secretaria_provisioning, "get_config_status", _noop_async(None))

    called = {"n": 0}

    async def fail_if_called(path, data):
        called["n"] += 1
        return {}

    monkeypatch.setattr(billing_service, "_stripe_post", fail_if_called)

    owner_a_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.post(
        "/doctor/onboarding/attempts",
        headers=_bearer(owner_a_token),
        json={"attempt_id": str(uuid4()), "result": "pass", "phone_number_id": "5511999990000"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["onboarding_state"] == "conectado"
    assert called["n"] == 0


# ===========================================================================================
# (b1) services.onboarding_sync.test_window_email_due -- pure due-condition matrix
# ===========================================================================================


def _settings(**overrides) -> SimpleNamespace:
    base = dict(STRIPE_TRIAL_PERIOD_DAYS=30)
    base.update(overrides)
    return SimpleNamespace(**base)


def _due_tenant(**overrides) -> Tenant:
    base = dict(
        clinic_name="Due Test Clinic",
        onboarding_state=onboarding.STATE_AQUECIMENTO,
        test_window_started_at=datetime.now(UTC) - timedelta(days=31),
        test_window_notified_at=None,
    )
    base.update(overrides)
    return Tenant(**base)


def _due_entitlement(**overrides) -> Entitlement:
    base = dict(plan="secretaria_basico", status="canceled", stripe_subscription_id="sub_due")
    base.update(overrides)
    return Entitlement(tenant_id=uuid4(), **base)


#: Short alias to keep the assertions below on one line each.
_due = onboarding_sync.test_window_email_due


def test_due_true_for_expired_unconnected_paid_secretaria_even_when_canceled():
    """The core due condition deliberately does NOT require ent.status active/trialing --
    by the deadline the subscription is typically already auto-cancelled
    (trial_will_end), and that IS the population this email targets."""
    assert _due(_due_tenant(), _due_entitlement(), _settings()) is True


def test_due_false_when_connected():
    tenant = _due_tenant(onboarding_state=onboarding.STATE_CONECTADO)
    assert _due(tenant, _due_entitlement(), _settings()) is False


def test_due_false_when_ativo():
    tenant = _due_tenant(onboarding_state=onboarding.STATE_ATIVO)
    assert _due(tenant, _due_entitlement(), _settings()) is False


def test_due_false_when_already_notified():
    tenant = _due_tenant(test_window_notified_at=datetime.now(UTC))
    assert _due(tenant, _due_entitlement(), _settings()) is False


def test_due_false_when_no_subscription_id():
    ent = _due_entitlement(stripe_subscription_id=None)
    assert _due(_due_tenant(), ent, _settings()) is False


def test_due_false_when_no_entitlement_row():
    assert _due(_due_tenant(), None, _settings()) is False


def test_due_false_when_days_zero():
    settings = _settings(STRIPE_TRIAL_PERIOD_DAYS=0)
    assert _due(_due_tenant(), _due_entitlement(), settings) is False


def test_due_false_when_plan_precheck_only():
    ent = _due_entitlement(plan="precheck")
    assert _due(_due_tenant(), ent, _settings()) is False


def test_due_false_when_window_not_started():
    tenant = _due_tenant(test_window_started_at=None)
    assert _due(tenant, _due_entitlement(), _settings()) is False


def test_due_false_before_deadline():
    tenant = _due_tenant(test_window_started_at=datetime.now(UTC) - timedelta(days=5))
    assert _due(tenant, _due_entitlement(), _settings()) is False


def test_due_true_handles_naive_started_at():
    """SQLite round-trips a stored aware datetime as naive; the comparison must still work
    (same idiom as services/onboarding_sync.py's other datetime comparisons)."""
    naive_started = (datetime.now(UTC) - timedelta(days=31)).replace(tzinfo=None)
    tenant = _due_tenant(test_window_started_at=naive_started)
    assert _due(tenant, _due_entitlement(), _settings()) is True


# ===========================================================================================
# (b2) GET /internal/onboarding/tenants -- wiring (due-logic itself covered by b1 above)
# ===========================================================================================


async def test_internal_onboarding_tenants_test_window_fields_wired(client, monkeypatch):
    import brain_api.api.internal as internal_api

    fake_settings = SimpleNamespace(
        SECRETARIA_API_KEY="pair-key",
        SECRETARIA_API_KEY_PREVIOUS="",
        STRIPE_TRIAL_PERIOD_DAYS=45,
        FRONTEND_BASE_URL="https://portal.example.com",
    )
    monkeypatch.setattr(internal_api, "get_settings", lambda: fake_settings)
    # The due-boolean LOGIC is exhaustively covered above; here we only prove the endpoint
    # wires the helper's result + the two settings-derived fields onto every row.
    monkeypatch.setattr(onboarding_sync, "test_window_email_due", lambda t, e, s: True)

    resp = await client.get(
        "/internal/onboarding/tenants", headers={"X-Internal-Api-Key": "pair-key"}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items  # both seeded tenants start non-'ativo'
    for item in items:
        assert item["test_window_email_due"] is True
        assert item["test_window_days"] == 45
        assert item["test_window_restart_url"] == "https://portal.example.com/app/reativar"


# ===========================================================================================
# (c) POST /internal/onboarding/tenants/{id}/events -- test_window_email_sent (one-shot)
# ===========================================================================================


async def test_internal_event_test_window_email_sent_one_shot(client, monkeypatch):
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = (await _tenant_ids(client, admin_token))[CLINIC_B]

    first_at = datetime.now(UTC).isoformat()
    r1 = await client.post(
        f"/internal/onboarding/tenants/{tenant_b_id}/events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"event": "test_window_email_sent", "at": first_at},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json() == {"applied": True}

    second_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    r2 = await client.post(
        f"/internal/onboarding/tenants/{tenant_b_id}/events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"event": "test_window_email_sent", "at": second_at},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"applied": False}  # one-shot: already set -> no-op.

    owner_b_token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    tw = await client.get("/doctor/onboarding/test-window", headers=_bearer(owner_b_token))
    assert tw.status_code == 200, tw.text
    assert tw.json()["notified"] is True


# ===========================================================================================
# (d) GET/POST /doctor/onboarding/test-window
# ===========================================================================================


async def test_get_test_window_default_state_not_applicable(client):
    """Tenant A (seeded on plan=precheck, no secretaria) reads as not-applicable, with no
    Stripe/settings dependency issues."""
    owner_a_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/doctor/onboarding/test-window", headers=_bearer(owner_a_token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applicable"] is False
    assert body["can_restart"] is False
    assert body["notified"] is False
    assert body["onboarding_state"] == "pending"


async def test_restart_test_window_live_trialing_extends_trial_end(client, monkeypatch):
    import brain_api.api.onboarding as onboarding_api

    monkeypatch.setattr(
        onboarding_api, "get_settings", lambda: SimpleNamespace(STRIPE_TRIAL_PERIOD_DAYS=30)
    )
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = (await _tenant_ids(client, admin_token))[CLINIC_B]
    await _link_and_set_plan(
        client, tenant_b_id, customer_id="cus_restart_live", subscription_id="sub_restart_live"
    )
    # Mark it already-notified beforehand so the restart's reset is observable.
    await client.post(
        f"/internal/onboarding/tenants/{tenant_b_id}/events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"event": "test_window_email_sent", "at": datetime.now(UTC).isoformat()},
    )

    async def fake_stripe_get(path):
        if path.startswith("/v1/payment_methods"):
            assert "customer=cus_restart_live" in path
            return {"data": [{"id": "pm_live_1"}]}
        if path == "/v1/subscriptions/sub_restart_live":
            return {"status": "trialing", "id": "sub_restart_live"}
        raise AssertionError(f"unexpected _stripe_get path: {path}")

    captured: dict = {}

    async def fake_stripe_post(path, data):
        captured["path"] = path
        captured["data"] = data
        return {}

    monkeypatch.setattr(billing_service, "_stripe_get", fake_stripe_get)
    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_post)

    owner_b_token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    before = datetime.now(UTC)
    resp = await client.post(
        "/doctor/onboarding/test-window/restart", headers=_bearer(owner_b_token)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restarted"] is True
    assert body["payment_method_present"] is True

    assert captured["path"] == "/v1/subscriptions/sub_restart_live"
    assert captured["data"]["proration_behavior"] == "none"
    assert captured["data"]["cancel_at"] == ""
    expected_deadline = int((before + timedelta(days=30)).timestamp())
    assert abs(int(captured["data"]["trial_end"]) - expected_deadline) <= 5

    # The subscription id itself never changed (live-extend branch).
    ent = await _admin_entitlements(client, tenant_b_id)
    assert ent["stripe_subscription_id"] == "sub_restart_live"

    tw = await client.get("/doctor/onboarding/test-window", headers=_bearer(owner_b_token))
    assert tw.json()["notified"] is False  # reset by the restart


async def test_restart_test_window_canceled_creates_new_subscription(client, monkeypatch):
    import brain_api.api.onboarding as onboarding_api

    monkeypatch.setattr(
        onboarding_api, "get_settings", lambda: SimpleNamespace(STRIPE_TRIAL_PERIOD_DAYS=30)
    )
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = (await _tenant_ids(client, admin_token))[CLINIC_B]
    await _link_and_set_plan(
        client, tenant_b_id, customer_id="cus_restart_dead", subscription_id="sub_restart_dead_OLD"
    )
    # The trial ran out: Stripe auto-cancelled (mirrors trial_will_end -> subscription.deleted).
    deleted = await _post_webhook(
        client,
        _event(
            "evt_restart_dead_deleted",
            "customer.subscription.deleted",
            {"customer": "cus_restart_dead"},
        ),
    )
    assert deleted.status_code == 200
    await client.post(
        f"/internal/onboarding/tenants/{tenant_b_id}/events",
        headers={"X-Internal-Api-Key": "pair-key"},
        json={"event": "test_window_email_sent", "at": datetime.now(UTC).isoformat()},
    )

    async def fake_stripe_get(path):
        if path.startswith("/v1/payment_methods"):
            return {"data": []}  # no saved card this time
        if path == "/v1/subscriptions/sub_restart_dead_OLD":
            return {"status": "canceled", "id": "sub_restart_dead_OLD"}
        raise AssertionError(f"unexpected _stripe_get path: {path}")

    captured: dict = {}

    async def fake_stripe_post(path, data):
        captured["path"] = path
        captured["data"] = data
        return {"id": "sub_restart_dead_NEW"}

    monkeypatch.setattr(billing_service, "_stripe_get", fake_stripe_get)
    monkeypatch.setattr(billing_service, "_stripe_post", fake_stripe_post)

    owner_b_token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    resp = await client.post(
        "/doctor/onboarding/test-window/restart", headers=_bearer(owner_b_token)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["restarted"] is True
    assert body["payment_method_present"] is False

    assert captured["path"] == "/v1/subscriptions"
    data = captured["data"]
    assert data["customer"] == "cus_restart_dead"
    assert data["items[0][price]"] == "price_ferro"
    assert data["items[0][quantity]"] == "1"
    assert data["trial_period_days"] == "30"
    assert data["proration_behavior"] == "none"
    assert data["metadata[tenant_id]"] == tenant_b_id
    assert "default_payment_method" not in data  # no saved card -> omitted

    ent = await _admin_entitlements(client, tenant_b_id)
    assert ent["stripe_subscription_id"] == "sub_restart_dead_NEW"

    tw = await client.get("/doctor/onboarding/test-window", headers=_bearer(owner_b_token))
    assert tw.json()["notified"] is False  # reset by the restart


async def test_restart_test_window_three_guards(client, monkeypatch):
    import brain_api.api.onboarding as onboarding_api

    monkeypatch.setattr(
        onboarding_api, "get_settings", lambda: SimpleNamespace(STRIPE_TRIAL_PERIOD_DAYS=30)
    )

    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = (await _tenant_ids(client, admin_token))[CLINIC_B]

    # Guard 1: test_window_not_applicable -- tenant A is seeded on plan=precheck.
    owner_a_token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    wrong_plan = await client.post(
        "/doctor/onboarding/test-window/restart", headers=_bearer(owner_a_token)
    )
    assert wrong_plan.status_code == 409
    assert wrong_plan.json()["detail"] == "test_window_not_applicable"

    # Guard 1b: test_window_not_applicable -- days == 0, even with a valid secretaria plan.
    await _link_and_set_plan(
        client, tenant_b_id, customer_id="cus_guard_days0", subscription_id="sub_guard_days0"
    )
    monkeypatch.setattr(
        onboarding_api, "get_settings", lambda: SimpleNamespace(STRIPE_TRIAL_PERIOD_DAYS=0)
    )
    owner_b_token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    days_zero = await client.post(
        "/doctor/onboarding/test-window/restart", headers=_bearer(owner_b_token)
    )
    assert days_zero.status_code == 409
    assert days_zero.json()["detail"] == "test_window_not_applicable"
    monkeypatch.setattr(
        onboarding_api, "get_settings", lambda: SimpleNamespace(STRIPE_TRIAL_PERIOD_DAYS=30)
    )

    # Guard 2: already_connected -- drive tenant B to 'conectado' via a real attempt.
    monkeypatch.setattr(
        secretaria_provisioning,
        "connect_whatsapp",
        _noop_async(secretaria_provisioning.CONNECTION_OK),
    )
    monkeypatch.setattr(secretaria_provisioning, "send_notification_email", _noop_async(True))
    monkeypatch.setattr(secretaria_provisioning, "get_config_status", _noop_async(None))
    attempt = await client.post(
        "/doctor/onboarding/attempts",
        headers=_bearer(owner_b_token),
        json={"attempt_id": str(uuid4()), "result": "pass", "phone_number_id": "5511999990000"},
    )
    assert attempt.status_code == 200, attempt.text
    assert attempt.json()["onboarding_state"] == "conectado"

    already_connected = await client.post(
        "/doctor/onboarding/test-window/restart", headers=_bearer(owner_b_token)
    )
    assert already_connected.status_code == 409
    assert already_connected.json()["detail"] == "already_connected"

    # Guard 3: checkout_required -- a secretaria-plan tenant with NO Stripe customer yet
    # (admin PATCH assigns the plan directly, no Stripe linkage at all).
    tenant_a_id = (await _tenant_ids(client, admin_token))[CLINIC_A]
    patch_resp = await client.patch(
        f"/admin/tenants/{tenant_a_id}/entitlements",
        headers=_bearer(admin_token),
        json={"plan": "secretaria_basico", "secretaria_enabled": True},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    no_checkout = await client.post(
        "/doctor/onboarding/test-window/restart", headers=_bearer(owner_a_token)
    )
    assert no_checkout.status_code == 409
    assert no_checkout.json()["detail"] == "checkout_required"


# ===========================================================================================
# (e) services.signup.provision_tenant_from_intent sets test_window_started_at
# ===========================================================================================


async def test_provision_tenant_from_intent_sets_test_window_started_at(client, monkeypatch):
    intent_id = await _create_intent(
        client, email="test.window.signup@example.com", catalog_ids=["secretaria_basico"]
    )
    captured: dict = {}
    _install_fake_stripe_httpx(
        monkeypatch, captured, {"id": "cs_tw_signup", "url": "https://checkout.stripe.test/tw"}
    )
    checkout = await client.post("/public/checkout-sessions", json={"intent_id": intent_id})
    assert checkout.status_code == 200, checkout.text

    before = datetime.now(UTC)
    obj = {
        "customer": "cus_tw_signup",
        "subscription": "sub_tw_signup",
        "metadata": {"kind": "signup_intent", "signup_intent_id": intent_id},
    }
    resp = await _post_webhook(client, _event("evt_tw_signup", "checkout.session.completed", obj))
    assert resp.status_code == 200, resp.text

    login = await client.post(
        "/auth/token",
        json={"email": "test.window.signup@example.com", "password": SIGNUP_PASSWORD},
    )
    assert login.status_code == 200, login.text
    owner_token = login.json()["access_token"]

    tw = await client.get("/doctor/onboarding/test-window", headers=_bearer(owner_token))
    assert tw.status_code == 200, tw.text
    started_at_raw = tw.json()["started_at"]
    assert started_at_raw is not None
    started_at = datetime.fromisoformat(started_at_raw.replace("Z", "+00:00"))
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    assert started_at >= before - timedelta(seconds=5)


# ===========================================================================================
# (f) A genuine subscription-id change restarts the test window (alongside the existing
# charge_hardened_at/cancel_scheduled_at reset, tests/test_billing_phase1.py)
# ===========================================================================================


async def test_subscription_id_change_resets_test_window(db_session):
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    tenant.test_window_started_at = datetime.now(UTC) - timedelta(days=40)
    tenant.test_window_notified_at = datetime.now(UTC) - timedelta(days=10)

    ent = Entitlement(
        tenant_id=tenant.id,
        plan="secretaria_ferro",
        status="trialing",
        stripe_customer_id="cus_tw_reset",
        stripe_subscription_id="sub_tw_old",
    )
    db_session.add(ent)
    await db_session.commit()

    before_reset = datetime.now(UTC)
    now_ts = int(time.time())
    created = await billing_service.apply_stripe_event(
        db_session,
        "evt_tw_reset_created",
        "customer.subscription.created",
        {
            "id": "sub_tw_new",
            "customer": "cus_tw_reset",
            "status": "trialing",
            "current_period_start": now_ts,
            "current_period_end": now_ts + 86400,
            "items": {"data": [{"price": {"id": "price_ferro"}, "quantity": 1}]},
        },
    )
    assert created is True
    await db_session.refresh(tenant)

    assert tenant.test_window_notified_at is None
    started_at = tenant.test_window_started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    assert started_at >= before_reset - timedelta(seconds=2)


async def test_same_subscription_id_redelivery_does_not_reset_test_window(db_session):
    tenant = Tenant(clinic_name="X")
    db_session.add(tenant)
    await db_session.flush()
    original_started = datetime.now(UTC) - timedelta(days=5)
    tenant.test_window_started_at = original_started
    tenant.test_window_notified_at = None

    ent = Entitlement(
        tenant_id=tenant.id,
        plan="secretaria_ferro",
        status="active",
        stripe_customer_id="cus_tw_same",
        stripe_subscription_id="sub_tw_same",
    )
    db_session.add(ent)
    await db_session.commit()

    now_ts = int(time.time())
    updated = await billing_service.apply_stripe_event(
        db_session,
        "evt_tw_same_updated",
        "customer.subscription.updated",
        {
            "id": "sub_tw_same",
            "customer": "cus_tw_same",
            "status": "active",
            "current_period_start": now_ts,
            "current_period_end": now_ts + 86400,
            "items": {"data": [{"price": {"id": "price_ferro"}, "quantity": 1}]},
        },
    )
    assert updated is True
    await db_session.refresh(tenant)

    started_at = tenant.test_window_started_at
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    assert started_at == original_started
