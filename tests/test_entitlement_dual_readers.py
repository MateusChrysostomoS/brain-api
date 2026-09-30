"""Readers of a clinic that has BOTH products (TASK C, spec §5.9): they must ask
`precheck_plan_of`, never `Entitlement.plan` alone."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from brain_api.models import Entitlement, Tenant
from brain_api.services import billing as billing_service, catalog, precheck_billing
from brain_api.services.entitlements import resolve_entitlement
from tests.billing_fakes import install_fake_settings


async def _clinic(db_session, secretaria_plan, precheck_plan) -> Entitlement:
    tenant = Tenant(clinic_name="Dual Clinic")
    db_session.add(tenant)
    await db_session.flush()
    state = catalog.compose_entitlement_state(secretaria_plan, precheck_plan)
    ent = Entitlement(
        tenant_id=tenant.id,
        status="active",
        manual_products=[],
        stripe_customer_id="cus_dual",
        stripe_subscription_id="sub_dual",
        **state,
    )
    db_session.add(ent)
    await db_session.commit()
    return ent


def _basic_quota() -> int:
    return catalog.get_plan(catalog.PLAN_PRECHECK_BASIC).base_limits[
        catalog.LIMIT_PRECHECK_CONSULTATIONS
    ]


async def test_entitlement_out_exposes_both_plans(db_session):
    ent = await _clinic(db_session, catalog.PLAN_SECRETARIA_BASICO, catalog.PLAN_PRECHECK_BASIC)
    out = await resolve_entitlement(db_session, ent.tenant_id)
    assert out.plan == catalog.PLAN_SECRETARIA_BASICO
    assert out.precheck_plan == catalog.PLAN_PRECHECK_BASIC
    assert out.secretaria_tier == catalog.TIER_BASICO
    assert out.products.precheck is True and out.products.secretaria is True
    assert out.limits[catalog.LIMIT_PRECHECK_CONSULTATIONS] == _basic_quota()


async def test_entitlement_out_precheck_plan_is_null_for_one_family(db_session):
    ent = await _clinic(db_session, None, catalog.PLAN_PRECHECK_BASIC)
    out = await resolve_entitlement(db_session, ent.tenant_id)
    assert out.plan == catalog.PLAN_PRECHECK_BASIC
    assert out.precheck_plan is None


async def test_usage_summary_reports_the_precheck_plan_not_the_anchor(db_session):
    ent = await _clinic(db_session, catalog.PLAN_SECRETARIA_BASICO, catalog.PLAN_PRECHECK_BASIC)
    summary = await precheck_billing.usage_summary(db_session, ent, datetime.now(UTC))
    assert summary.plan == catalog.PLAN_PRECHECK_BASIC
    assert summary.plan_name == "PreCheck Basic"
    assert summary.precheck_enabled is True
    assert summary.quota == _basic_quota()
    assert summary.enforced is (_basic_quota() > 0)


async def test_usage_summary_of_a_secretaria_only_clinic_has_no_precheck(db_session):
    ent = await _clinic(db_session, catalog.PLAN_SECRETARIA_BASICO, None)
    summary = await precheck_billing.usage_summary(db_session, ent, datetime.now(UTC))
    assert summary.precheck_enabled is False
    assert summary.quota == 0


async def test_topup_accepts_a_dual_clinic_and_refuses_secretaria_only(db_session, monkeypatch):
    install_fake_settings(monkeypatch)
    captured: dict = {}

    async def fake_post(path, data, *, idempotency_key=None):
        captured["data"] = data
        return {"url": "https://checkout.stripe.test/topup"}

    monkeypatch.setattr(billing_service, "_stripe_post", fake_post)

    dual = await _clinic(db_session, catalog.PLAN_SECRETARIA_BASICO, catalog.PLAN_PRECHECK_BASIC)
    url = await billing_service.create_precheck_topup_checkout_session(
        db_session, dual.tenant_id, 10
    )
    assert url == "https://checkout.stripe.test/topup"
    assert captured["data"]["line_items[0][price]"] == "price_topup"

    only_secretaria = await _clinic(db_session, catalog.PLAN_SECRETARIA_BASICO, None)
    with pytest.raises(HTTPException) as exc:
        await billing_service.create_precheck_topup_checkout_session(
            db_session, only_secretaria.tenant_id, 10
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "not_precheck_plan"
