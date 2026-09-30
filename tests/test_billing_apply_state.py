"""apply_subscription_state: previous row x subscription event -> next row (TASK C, spec §5.3).

Pure: builds transient Entitlement objects, no database.
"""

from uuid import uuid4

import pytest

from brain_api.models import Entitlement
from brain_api.services import billing as billing_service, catalog
from brain_api.services.billing import apply_subscription_state
from tests.billing_fakes import SEC_PRICES, install_fake_settings, make_sub


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    install_fake_settings(monkeypatch)


def _clinic(secretaria_plan=None, precheck_plan=None, *, manual=(), status="active", sub_id=None):
    state = catalog.compose_entitlement_state(secretaria_plan, precheck_plan)
    return Entitlement(
        tenant_id=uuid4(),
        status=status,
        manual_products=list(manual),
        stripe_subscription_id=sub_id,
        **state,
    )


def _view(ent: Entitlement) -> dict:
    return {
        "plan": ent.plan,
        "precheck_plan": ent.precheck_plan,
        "precheck": ent.precheck_enabled,
        "secretaria": ent.secretaria_enabled,
        "status": ent.status,
        "manual": list(ent.manual_products),
        "sub_id": ent.stripe_subscription_id,
    }


def test_live_statuses_are_values_stripe_webhook_can_write():
    assert billing_service.LIVE_SUBSCRIPTION_STATUSES == {"active", "trialing", "past_due"}
    assert billing_service.LIVE_SUBSCRIPTION_STATUSES <= set(billing_service._STATUS_MAP.values())


def test_courtesy_secretaria_plus_a_precheck_subscription_keeps_both_and_survives_renewal():
    ent = _clinic("secretaria_basico", manual=["secretaria"])
    sub = make_sub("price_pc_basic", sub_id="sub_2")

    first = apply_subscription_state(ent, sub)
    ent.stripe_subscription_id = "sub_2"  # what the webhook does after applying
    assert _view(ent) == {
        "plan": "secretaria_basico",
        "precheck_plan": "precheck_basic",
        "precheck": True,
        "secretaria": True,
        "status": "active",
        "manual": ["secretaria"],
        "sub_id": "sub_2",
    }
    assert first.secretaria_newly_paid is False  # secretarIA is not what the subscription carries

    before = _view(ent)
    apply_subscription_state(
        ent, make_sub("price_pc_basic", sub_id="sub_2")
    )  # renewal, same event 2x
    assert _view(ent) == before


def test_deleting_the_paid_precheck_subscription_leaves_the_manual_secretaria():
    ent = _clinic("secretaria_basico", manual=["secretaria"])
    apply_subscription_state(ent, make_sub("price_pc_basic", sub_id="sub_2"))
    ent.stripe_subscription_id = "sub_2"

    apply_subscription_state(ent, make_sub("price_pc_basic", sub_id="sub_2"), deleted=True)
    assert _view(ent) == {
        "plan": "secretaria_basico",
        "precheck_plan": None,
        "precheck": False,
        "secretaria": True,
        "status": "active",
        "manual": ["secretaria"],
        "sub_id": None,  # back to "courtesy clinic": it can subscribe again through checkout
    }
    assert ent.limits[catalog.LIMIT_PRECHECK_CONSULTATIONS] == 0


def test_manual_precheck_plus_a_secretaria_subscription_is_a_dual_row():
    ent = _clinic(None, "precheck_basic", manual=["precheck"])
    result = apply_subscription_state(ent, make_sub(*SEC_PRICES, sub_id="sub_3"))
    assert _view(ent)["plan"] == "secretaria_basico"
    assert ent.precheck_plan == "precheck_basic"
    assert ent.precheck_enabled and ent.secretaria_enabled
    assert ent.manual_products == ["precheck"]
    assert result.secretaria_newly_paid is True  # the clinic already had PreCheck


def test_a_live_subscription_converts_the_manual_family_it_carries():
    ent = _clinic("secretaria_basico", manual=["secretaria"])
    result = apply_subscription_state(ent, make_sub(*SEC_PRICES, sub_id="sub_4", status="trialing"))
    assert ent.manual_products == []
    assert ent.status == "trialing"
    assert result.secretaria_newly_paid is True  # manual -> paid: a fresh test window

    ent.stripe_subscription_id = "sub_4"
    apply_subscription_state(ent, make_sub(*SEC_PRICES, sub_id="sub_4"), deleted=True)
    assert _view(ent)["secretaria"] is False
    assert ent.status == "canceled"
    assert ent.plan == "secretaria_basico"  # the plan bought stays (restart_test_window needs it)


def test_cold_signup_event_on_an_inert_row_is_not_a_new_paid_family():
    inert = Entitlement(
        tenant_id=uuid4(),
        plan="free",
        status="inactive",
        precheck_enabled=False,
        secretaria_enabled=False,
        addons={},
        limits={},
        manual_products=[],
    )
    result = apply_subscription_state(inert, make_sub(*SEC_PRICES, status="trialing"))
    assert inert.secretaria_enabled is True and inert.plan == "secretaria_basico"
    assert result.secretaria_newly_paid is False  # no prior access: not an "add", no window restart


def test_replaying_the_event_of_a_subscription_already_paying_is_not_new():
    ent = _clinic("secretaria_basico", sub_id="sub_1")
    result = apply_subscription_state(ent, make_sub(*SEC_PRICES, sub_id="sub_1"))
    assert result.secretaria_newly_paid is False


def test_past_due_keeps_both_products_and_the_past_due_status():
    ent = _clinic("secretaria_basico", "precheck_basic", sub_id="sub_1")
    apply_subscription_state(ent, make_sub(*SEC_PRICES, "price_pc_basic", status="past_due"))
    assert ent.precheck_enabled and ent.secretaria_enabled
    assert ent.status == "past_due"


def test_non_live_subscription_never_converts_a_manual_product():
    ent = _clinic("secretaria_basico", manual=["secretaria"])
    apply_subscription_state(ent, make_sub(*SEC_PRICES, status="incomplete"))
    assert ent.secretaria_enabled is True
    assert ent.manual_products == ["secretaria"]
    assert ent.status == "active"


def test_unpaid_without_manual_switches_flags_off_and_keeps_the_plan():
    ent = _clinic("secretaria_basico", status="active", sub_id="sub_1")
    apply_subscription_state(ent, make_sub(*SEC_PRICES, status="unpaid"))
    assert not ent.precheck_enabled and not ent.secretaria_enabled
    assert ent.status == "inactive"  # fail closed, exactly as before
    assert ent.plan == "secretaria_basico"


def test_deleted_without_manual_keeps_the_plan_and_the_subscription_id():
    ent = _clinic("secretaria_basico", "precheck_basic", sub_id="sub_1")
    apply_subscription_state(ent, make_sub(*SEC_PRICES, "price_pc_basic"), deleted=True)
    assert _view(ent) == {
        "plan": "secretaria_basico",
        "precheck_plan": "precheck_basic",
        "precheck": False,
        "secretaria": False,
        "status": "canceled",
        "manual": [],
        "sub_id": "sub_1",
    }


def test_a_family_removed_from_the_subscription_turns_off():
    ent = _clinic("secretaria_basico", "precheck_basic", sub_id="sub_1")
    apply_subscription_state(ent, make_sub(*SEC_PRICES))
    assert ent.precheck_enabled is False
    assert ent.precheck_plan is None
    assert ent.secretaria_enabled is True and ent.plan == "secretaria_basico"


def test_an_unrecognized_subscription_only_updates_status_and_period():
    ent = _clinic("secretaria_basico", sub_id="sub_1")
    before = _view(ent)
    result = apply_subscription_state(ent, make_sub(("price_multipro", 1), status="past_due"))
    assert result.recognized is False
    assert {**_view(ent), "status": before["status"]} == before
    assert ent.status == "past_due"
    assert ent.period_end is not None


def test_manual_combo_converts_precheck_to_paid_tier():
    ent = _clinic("complete_clinic_combo", manual=["precheck", "secretaria"])
    apply_subscription_state(ent, make_sub("price_pc_start"))
    assert ent.plan == "secretaria_basico"
    assert ent.precheck_plan == "precheck_start"
    assert ent.precheck_enabled and ent.secretaria_enabled
    assert ent.manual_products == ["secretaria"]


def test_addons_and_quantity_come_from_the_live_subscription():
    ent = _clinic()
    apply_subscription_state(ent, make_sub(*SEC_PRICES, ("price_multipro", 3)))
    assert ent.addons[catalog.ADDON_MULTI_PROFESSIONAL] is True
    assert ent.limits[catalog.LIMIT_PROFESSIONALS] == 4
    assert ent.period_start is not None and ent.period_end is not None
