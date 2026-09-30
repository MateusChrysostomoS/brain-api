"""Writers that switch products on WITHOUT a subscription record them as manual
(TASK C, spec §5.3): courtesy redemption, POST /admin/tenants, admin PATCH."""

from uuid import UUID

from brain_api.models import Entitlement, Tenant
from brain_api.schemas.admin import EntitlementPatchIn
from brain_api.services import admin as admin_service, catalog
from tests.test_admin_tenant_create import _body, _create
from tests.test_courtesy_coupon import _cupom, _le_entitlement, _le_intent, _registra


async def test_courtesy_redemption_records_its_families_as_manual(client):
    await _cupom(plan_id=catalog.PLAN_COMPLETE_CLINIC_COMBO)
    intent_id = await _registra(client)
    resp = await client.post(
        "/public/courtesy-redemptions", json={"intent_id": intent_id, "code": "CORTESIA100"}
    )
    assert resp.status_code == 200, resp.text
    ent = await _le_entitlement((await _le_intent(intent_id)).tenant_id)
    assert ent.stripe_subscription_id is None
    assert ent.manual_products == ["precheck", "secretaria"]
    assert ent.precheck_plan is None


async def test_courtesy_precheck_only_is_manual_precheck(client):
    await _cupom(plan_id=catalog.PLAN_PRECHECK_BASIC)
    intent_id = await _registra(client)
    resp = await client.post(
        "/public/courtesy-redemptions", json={"intent_id": intent_id, "code": "CORTESIA100"}
    )
    assert resp.status_code == 200, resp.text
    ent = await _le_entitlement((await _le_intent(intent_id)).tenant_id)
    assert ent.manual_products == ["precheck"]


async def test_admin_created_test_clinic_records_manual_products(client):
    resp = await _create(client, _body(precheck=True, secretaria=False))
    assert resp.status_code == 201, resp.text
    ent = await _le_entitlement(UUID(resp.json()["tenant_id"]))
    assert ent.manual_products == ["precheck"]
    assert ent.stripe_subscription_id is None


async def _clinic(db_session, *, subscription: str | None, **fields) -> Entitlement:
    tenant = Tenant(clinic_name="Patch Clinic")
    db_session.add(tenant)
    await db_session.flush()
    ent = Entitlement(tenant_id=tenant.id, stripe_subscription_id=subscription, **fields)
    db_session.add(ent)
    await db_session.commit()
    return ent


async def test_admin_patch_without_a_subscription_records_manual_products(db_session):
    tenant = Tenant(clinic_name="No Row Yet")
    db_session.add(tenant)
    await db_session.commit()

    out = await admin_service.update_entitlement(
        db_session, tenant.id, EntitlementPatchIn(plan="secretaria_basico", status="active")
    )
    assert out.manual_products == ["secretaria"]
    assert out.precheck_plan is None

    out = await admin_service.update_entitlement(
        db_session, tenant.id, EntitlementPatchIn(precheck_enabled=True)
    )
    assert out.manual_products == ["precheck", "secretaria"]


async def test_admin_patch_with_a_subscription_leaves_manual_products_alone(db_session):
    ent = await _clinic(
        db_session,
        subscription="sub_live",
        plan=catalog.PLAN_PRECHECK_BASIC,
        status="active",
        precheck_enabled=True,
        manual_products=["precheck"],
    )
    out = await admin_service.update_entitlement(
        db_session, ent.tenant_id, EntitlementPatchIn(secretaria_enabled=True)
    )
    assert out.secretaria_enabled is True
    assert out.manual_products == ["precheck"]  # the next Stripe event still owns this row


async def test_admin_plan_patch_clears_the_precheck_tier_column(db_session):
    state = catalog.compose_entitlement_state(
        catalog.PLAN_SECRETARIA_BASICO, catalog.PLAN_PRECHECK_BASIC
    )
    ent = await _clinic(db_session, subscription="sub_dual", status="active", **state)
    out = await admin_service.update_entitlement(
        db_session, ent.tenant_id, EntitlementPatchIn(plan=catalog.PLAN_SECRETARIA_BASICO)
    )
    assert out.precheck_plan is None
    assert out.precheck_enabled is False  # a plan patch re-materializes ONE plan
