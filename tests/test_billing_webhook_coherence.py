"""Webhook coherence after TASK C (spec §5.6): both products in one subscription, the stale-id
guard on `deleted`/`updated`, and the test-window restart policy.

Prices come from tests/conftest.py's map: `price_ferro` = secretaria_basico (direct price),
`price_precheck` = precheck_basic. Events go through the REAL /webhooks/stripe route.
"""

import time
from datetime import UTC, datetime, timedelta

from tests.billing_fakes import read_window, set_entitlement, set_window
from tests.test_billing import _admin_entitlements, _event, _post_webhook, _tenant_ids
from tests.test_rbac import CLINIC_B

NOW = int(time.time())


def _sub(
    tenant_id: str, *price_ids: str, sub_id="sub_x", status="active", customer="cus_x"
) -> dict:
    return {
        "id": sub_id,
        "customer": customer,
        "status": status,
        "current_period_start": NOW,
        "current_period_end": NOW + 30 * 86400,
        "items": {"data": [{"price": {"id": p}, "quantity": 1} for p in price_ids]},
        "metadata": {"tenant_id": tenant_id},
    }


async def _deliver(client, event_id: str, event_type: str, obj: dict) -> None:
    resp = await _post_webhook(client, _event(event_id, event_type, obj))
    assert resp.status_code == 200, resp.text


async def test_adding_precheck_to_a_secretaria_subscription_yields_both_and_keeps_the_window(
    client,
):
    tenant = (await _tenant_ids(client))[CLINIC_B]
    await _deliver(client, "evt_a1", "customer.subscription.created", _sub(tenant, "price_ferro"))
    old = datetime.now(UTC) - timedelta(days=10)
    await set_window(tenant, started_at=old, notified_at=None)

    await _deliver(
        client,
        "evt_a2",
        "customer.subscription.updated",
        _sub(tenant, "price_ferro", "price_precheck"),
    )

    ent = await _admin_entitlements(client, tenant)
    assert ent["secretaria_enabled"] is True and ent["precheck_enabled"] is True
    assert ent["plan"] == "secretaria_basico"
    assert ent["precheck_plan"] == "precheck_basic"
    assert ent["stripe_subscription_id"] == "sub_x"
    started, _ = await read_window(tenant)
    assert abs((started - old).total_seconds()) < 1  # PreCheck added: the window never restarts


async def test_adding_secretaria_to_a_precheck_subscription_restarts_the_window_once(client):
    tenant = (await _tenant_ids(client))[CLINIC_B]
    await _deliver(
        client, "evt_b1", "customer.subscription.created", _sub(tenant, "price_precheck")
    )
    old = datetime.now(UTC) - timedelta(days=10)
    await set_window(tenant, started_at=old, notified_at=datetime.now(UTC))

    both = _sub(tenant, "price_precheck", "price_ferro")
    await _deliver(client, "evt_b2", "customer.subscription.updated", both)
    started, notified = await read_window(tenant)
    assert started > old + timedelta(days=9)
    assert notified is None

    # a renewal `updated` of the SAME subscription must not restart it again
    restarted_at = started
    await _deliver(client, "evt_b3", "customer.subscription.updated", both)
    again, _ = await read_window(tenant)
    assert again == restarted_at
    ent = await _admin_entitlements(client, tenant)
    assert ent["secretaria_enabled"] is True and ent["precheck_enabled"] is True


async def test_stale_deleted_of_an_old_subscription_is_ignored(client):
    tenant = (await _tenant_ids(client))[CLINIC_B]
    await _deliver(
        client,
        "evt_c1",
        "customer.subscription.created",
        _sub(tenant, "price_ferro", sub_id="sub_new"),
    )

    await _deliver(
        client,
        "evt_c2",
        "customer.subscription.deleted",
        {"id": "sub_old", "customer": "cus_x", "metadata": {"tenant_id": tenant}},
    )
    ent = await _admin_entitlements(client, tenant)
    assert ent["status"] == "active"
    assert ent["secretaria_enabled"] is True
    assert ent["stripe_subscription_id"] == "sub_new"


async def test_stale_updated_of_an_old_subscription_is_ignored(client):
    tenant = (await _tenant_ids(client))[CLINIC_B]
    await _deliver(
        client,
        "evt_d1",
        "customer.subscription.created",
        _sub(tenant, "price_ferro", sub_id="sub_cur"),
    )

    await _deliver(
        client,
        "evt_d2",
        "customer.subscription.updated",
        _sub(tenant, "price_precheck", sub_id="sub_other", status="canceled"),
    )
    ent = await _admin_entitlements(client, tenant)
    assert ent["stripe_subscription_id"] == "sub_cur"
    assert ent["secretaria_enabled"] is True
    assert ent["precheck_enabled"] is False
    assert ent["status"] == "active"


async def test_deleted_of_the_current_subscription_switches_both_products_off(client):
    tenant = (await _tenant_ids(client))[CLINIC_B]
    both = _sub(tenant, "price_ferro", "price_precheck")
    await _deliver(client, "evt_e1", "customer.subscription.created", both)
    await _deliver(
        client, "evt_e2", "customer.subscription.deleted", {**both, "status": "canceled"}
    )
    ent = await _admin_entitlements(client, tenant)
    assert ent["status"] == "canceled"
    assert ent["secretaria_enabled"] is False and ent["precheck_enabled"] is False
    assert ent["plan"] == "secretaria_basico"  # the plan bought stays
    assert ent["stripe_subscription_id"] == "sub_x"


async def test_deleted_without_a_subscription_id_still_applies(client):
    """The minimal legacy shape (tests/test_billing.py, tests/test_test_window.py) has no `id`."""
    tenant = (await _tenant_ids(client))[CLINIC_B]
    await _deliver(client, "evt_f1", "customer.subscription.created", _sub(tenant, "price_ferro"))
    await _deliver(
        client,
        "evt_f2",
        "customer.subscription.deleted",
        {"customer": "cus_x", "metadata": {"tenant_id": tenant}},
    )
    assert (await _admin_entitlements(client, tenant))["status"] == "canceled"


async def test_a_new_subscription_after_the_old_one_died_is_applied_and_restarts_the_window(client):
    tenant = (await _tenant_ids(client))[CLINIC_B]
    await _deliver(
        client,
        "evt_g1",
        "customer.subscription.created",
        _sub(tenant, "price_ferro", sub_id="sub_old"),
    )
    await _deliver(
        client,
        "evt_g2",
        "customer.subscription.deleted",
        {"id": "sub_old", "customer": "cus_x", "metadata": {"tenant_id": tenant}},
    )
    await set_entitlement(tenant, charge_hardened_at=datetime.now(UTC))
    old = datetime.now(UTC) - timedelta(days=30)
    await set_window(tenant, started_at=old, notified_at=datetime.now(UTC))

    await _deliver(
        client,
        "evt_g3",
        "customer.subscription.created",
        _sub(tenant, "price_ferro", sub_id="sub_new"),
    )

    ent = await _admin_entitlements(client, tenant)
    assert ent["stripe_subscription_id"] == "sub_new"
    assert ent["secretaria_enabled"] is True and ent["status"] == "active"
    started, notified = await read_window(tenant)
    assert started > old + timedelta(days=29) and notified is None
