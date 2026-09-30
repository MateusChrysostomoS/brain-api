"""restart_test_window after TASK C (spec §5.8): never freezes a paying subscription, and
re-creates BOTH products of a dual clinic."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from brain_api.models import Entitlement
from brain_api.services import billing as billing_service, catalog
from tests.billing_fakes import (
    install_fake_settings,
    read_window,
    set_entitlement,
    set_window,
)
from tests.test_billing import _event, _post_webhook
from tests.test_rbac import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    CLINIC_B,
    OWNER_B_EMAIL,
    OWNER_B_PASSWORD,
    _bearer,
    _token,
)
from tests.test_test_window import _link_and_set_plan
from tests.test_usage_events import _set_pair_key, _tenant_ids


def _fakes(monkeypatch, *, live_status: str, sub_id: str):
    posts: list[tuple[str, dict]] = []

    async def fake_get(path):
        if path.startswith("/v1/payment_methods"):
            return {"data": [{"id": "pm_1"}]}
        if path == f"/v1/subscriptions/{sub_id}":
            return {"status": live_status, "id": sub_id}
        raise AssertionError(f"unexpected _stripe_get path: {path}")

    async def fake_post(path, data):
        posts.append((path, data))
        return {"id": "sub_NEW"}

    monkeypatch.setattr(billing_service, "_stripe_get", fake_get)
    monkeypatch.setattr(billing_service, "_stripe_post", fake_post)
    return posts


async def _setup(client, monkeypatch, subscription_id: str, status: str):
    import brain_api.api.onboarding as onboarding_api

    monkeypatch.setattr(
        onboarding_api, "get_settings", lambda: SimpleNamespace(STRIPE_TRIAL_PERIOD_DAYS=30)
    )
    _set_pair_key(monkeypatch)
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b = (await _tenant_ids(client, admin_token))[CLINIC_B]
    await _link_and_set_plan(
        client,
        tenant_b,
        customer_id=f"cus_{subscription_id}",
        subscription_id=subscription_id,
        status=status,
    )
    return tenant_b


async def test_restart_on_an_active_subscription_never_sends_trial_end(client, monkeypatch):
    tenant_b = await _setup(client, monkeypatch, "sub_act", "active")
    posts = _fakes(monkeypatch, live_status="active", sub_id="sub_act")
    old = datetime.now(UTC) - timedelta(days=40)
    await set_window(tenant_b, started_at=old, notified_at=datetime.now(UTC))

    token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    resp = await client.post("/doctor/onboarding/test-window/restart", headers=_bearer(token))

    assert resp.status_code == 200, resp.text
    assert resp.json()["restarted"] is True
    assert posts == [], "a subscription that already charges must not be sent a trial_end"
    started, notified = await read_window(tenant_b)
    assert started > old + timedelta(days=39) and notified is None  # the LOCAL window did restart


async def test_restart_on_a_trialing_subscription_still_extends_the_trial(client, monkeypatch):
    await _setup(client, monkeypatch, "sub_trial", "trialing")
    posts = _fakes(monkeypatch, live_status="trialing", sub_id="sub_trial")
    token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    resp = await client.post("/doctor/onboarding/test-window/restart", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert [path for path, _ in posts] == ["/v1/subscriptions/sub_trial"]
    assert "trial_end" in posts[0][1]


async def test_restart_recreates_both_products_of_a_dual_clinic(client, monkeypatch):
    tenant_b = await _setup(client, monkeypatch, "sub_dual_OLD", "trialing")
    await set_entitlement(tenant_b, precheck_plan="precheck_basic", precheck_enabled=True)
    # the trial ran out: Stripe cancelled the subscription (the id matches the current one)
    deleted = await _post_webhook(
        client,
        _event(
            "evt_dual_deleted",
            "customer.subscription.deleted",
            {
                "id": "sub_dual_OLD",
                "customer": "cus_sub_dual_OLD",
                "metadata": {"tenant_id": tenant_b},
            },
        ),
    )
    assert deleted.status_code == 200
    posts = _fakes(monkeypatch, live_status="canceled", sub_id="sub_dual_OLD")

    token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    resp = await client.post("/doctor/onboarding/test-window/restart", headers=_bearer(token))

    assert resp.status_code == 200, resp.text
    ((path, data),) = posts
    assert path == "/v1/subscriptions"
    assert (
        data["items[0][price]"] == "price_ferro" and data["items[0][quantity]"] == "1"
    )  # secretarIA (conftest map)
    assert (
        data["items[1][price]"] == "price_precheck" and data["items[1][quantity]"] == "1"
    )  # + the PreCheck tier
    assert data["trial_period_days"] == "30"


# --- paid_selections_for_entitlement (pure) ------------------------------------------------------


@pytest.fixture
def _settings(monkeypatch):
    install_fake_settings(monkeypatch)


def _ent(secretaria_plan=None, precheck_plan=None, *, manual=(), addons=None) -> Entitlement:
    state = catalog.compose_entitlement_state(secretaria_plan, precheck_plan)
    if addons is not None:
        state["addons"] = {**state["addons"], **addons}
    return Entitlement(tenant_id=uuid4(), status="active", manual_products=list(manual), **state)


def _plans(selections):
    return [s.plan_id for s in selections]


def test_secretaria_only(_settings):
    assert _plans(billing_service.paid_selections_for_entitlement(_ent("secretaria_basico"))) == [
        "secretaria_basico"
    ]


def test_a_dual_clinic_pays_for_both_and_addons_ride_on_the_first(_settings):
    ent = _ent("secretaria_basico", "precheck_basic", addons={"multi_professional": True})
    selections = billing_service.paid_selections_for_entitlement(ent)
    assert _plans(selections) == ["secretaria_basico", "precheck_basic"]
    assert selections[0].addon_ids == ("multi_professional",)
    assert selections[1].addon_ids == ()


def test_a_manual_family_is_not_re_created_as_a_paid_item(_settings):
    ent = _ent("secretaria_basico", "precheck_basic", manual=["precheck"])
    assert _plans(billing_service.paid_selections_for_entitlement(ent)) == ["secretaria_basico"]


def test_the_combo_is_one_selection(_settings):
    ent = _ent("complete_clinic_combo")
    assert _plans(billing_service.paid_selections_for_entitlement(ent)) == ["complete_clinic_combo"]


def test_nothing_paid_means_nothing_to_re_create(_settings):
    ent = _ent("secretaria_basico", "precheck_basic", manual=["secretaria", "precheck"])
    assert billing_service.paid_selections_for_entitlement(ent) == []
