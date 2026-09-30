"""upgrade_precheck_plan for a combo tenant and for a clinic with BOTH products (TASK C §5.7)."""

import pytest
from fastapi import HTTPException

from brain_api.services import billing as billing_service, catalog
from tests.billing_fakes import SEC_PRICES, install_fake_settings, make_clinic, make_sub


def _wire(monkeypatch, live: dict):
    calls: dict = {"get": [], "post": []}

    async def fake_get(path):
        calls["get"].append(path)
        return live

    async def fake_post(path, data, *, idempotency_key=None):
        calls["post"].append((path, data))
        return {"id": "sub_1", "status": "active"}

    monkeypatch.setattr(billing_service, "_stripe_get", fake_get)
    monkeypatch.setattr(billing_service, "_stripe_post", fake_post)
    return calls


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    install_fake_settings(monkeypatch)


async def test_a_combo_tenant_cannot_swap_and_nothing_reaches_stripe(db_session, monkeypatch):
    calls = _wire(monkeypatch, make_sub("price_combo"))
    ent = await make_clinic(db_session, "complete_clinic_combo", None)
    with pytest.raises(HTTPException) as exc:
        await billing_service.upgrade_precheck_plan(db_session, ent.tenant_id, "precheck_advanced")
    assert (exc.value.status_code, exc.value.detail) == (409, "combo_plan_not_swappable")
    assert calls == {"get": [], "post": []}


async def test_a_dual_clinic_swaps_only_its_precheck_tier(db_session, monkeypatch):
    calls = _wire(monkeypatch, make_sub(*SEC_PRICES, "price_pc_basic"))
    ent = await make_clinic(db_session, "secretaria_basico", "precheck_basic")
    secretaria_limits = {
        k: v for k, v in ent.limits.items() if k != catalog.LIMIT_PRECHECK_CONSULTATIONS
    }

    summary = await billing_service.upgrade_precheck_plan(
        db_session, ent.tenant_id, "precheck_advanced"
    )

    ((path, data),) = calls["post"]
    assert path == "/v1/subscriptions/sub_1"
    assert data["items[0][id]"] == "si_price_pc_basic"  # the PreCheck item, found by its price
    assert data["items[0][price]"] == "price_pc_adv"
    assert data["proration_behavior"] == "create_prorations"

    await db_session.refresh(ent)
    assert ent.plan == "secretaria_basico"  # the anchor stays the secretarIA plan
    assert ent.precheck_plan == "precheck_advanced"
    assert ent.precheck_enabled and ent.secretaria_enabled
    advanced_quota = catalog.get_plan("precheck_advanced").base_limits[
        catalog.LIMIT_PRECHECK_CONSULTATIONS
    ]
    assert ent.limits[catalog.LIMIT_PRECHECK_CONSULTATIONS] == advanced_quota
    assert {
        k: v for k, v in ent.limits.items() if k != catalog.LIMIT_PRECHECK_CONSULTATIONS
    } == secretaria_limits
    assert summary.plan == "precheck_advanced" and summary.quota == advanced_quota


async def test_a_dual_clinic_already_on_the_target_tier_is_refused(db_session, monkeypatch):
    _wire(monkeypatch, make_sub(*SEC_PRICES, "price_pc_basic"))
    ent = await make_clinic(db_session, "secretaria_basico", "precheck_basic")
    with pytest.raises(HTTPException) as exc:
        await billing_service.upgrade_precheck_plan(db_session, ent.tenant_id, "precheck_basic")
    assert (exc.value.status_code, exc.value.detail) == (409, "already_on_plan")


async def test_a_secretaria_only_clinic_has_nothing_to_swap(db_session, monkeypatch):
    _wire(monkeypatch, make_sub(*SEC_PRICES))
    ent = await make_clinic(db_session, "secretaria_basico", None)
    with pytest.raises(HTTPException) as exc:
        await billing_service.upgrade_precheck_plan(db_session, ent.tenant_id, "precheck_advanced")
    assert (exc.value.status_code, exc.value.detail) == (409, "not_precheck_plan")


async def test_a_precheck_only_clinic_keeps_todays_behavior(db_session, monkeypatch):
    _wire(monkeypatch, make_sub("price_pc_basic"))
    ent = await make_clinic(db_session, None, "precheck_basic")
    await billing_service.upgrade_precheck_plan(db_session, ent.tenant_id, "precheck_advanced")
    await db_session.refresh(ent)
    assert ent.plan == "precheck_advanced"
    assert ent.precheck_plan is None
    assert ent.precheck_enabled and not ent.secretaria_enabled
