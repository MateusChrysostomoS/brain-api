"""Derivation of the entitlement state from a subscription's items (TASK C, spec §5.2).

The matrix pins every row of the spec's table. Row 4 is the one that used to be wrong: the
old loop overwrote `plan_id` per item, so the LAST item decided the product.
"""

import pytest

from brain_api.services import billing as billing_service, catalog
from tests.billing_fakes import SEC_PRICES, install_fake_settings, make_sub


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    install_fake_settings(monkeypatch)


def _quota(plan_id: str) -> int:
    return catalog.get_plan(plan_id).base_limits[catalog.LIMIT_PRECHECK_CONSULTATIONS]


# (row, items, plan, precheck_plan, precheck, secretaria, plan whose quota applies | None = 0)
MATRIX = [
    (1, ["price_pc_basic"], "precheck_basic", None, True, False, "precheck_basic"),
    (2, [*SEC_PRICES], "secretaria_basico", None, False, True, None),
    (
        3,
        ["price_pc_basic", *SEC_PRICES],
        "secretaria_basico",
        "precheck_basic",
        True,
        True,
        "precheck_basic",
    ),
    (
        4,
        [*SEC_PRICES, "price_pc_start"],
        "secretaria_basico",
        "precheck_start",
        True,
        True,
        "precheck_start",
    ),
    (
        5,
        ["price_sec_pat", "price_pc_adv"],
        "secretaria_basico",
        "precheck_advanced",
        True,
        True,
        "precheck_advanced",
    ),
    (6, ["price_combo"], "complete_clinic_combo", None, True, True, "complete_clinic_combo"),
    (
        7,
        ["price_combo", "price_pc_basic"],
        "complete_clinic_combo",
        None,
        True,
        True,
        "complete_clinic_combo",
    ),
    (
        8,
        ["price_pc_basic", "price_pc_adv"],
        "precheck_advanced",
        None,
        True,
        False,
        "precheck_advanced",
    ),
    (
        11,
        ["price_unknown", "price_pc_basic"],
        "precheck_basic",
        None,
        True,
        False,
        "precheck_basic",
    ),
]


@pytest.mark.parametrize(
    ("row", "items", "plan", "precheck_plan", "precheck", "secretaria", "quota_plan"),
    MATRIX,
    ids=[f"row{m[0]}" for m in MATRIX],
)
def test_state_matrix(row, items, plan, precheck_plan, precheck, secretaria, quota_plan):
    state = billing_service._state_from_subscription(make_sub(*items))
    assert state is not None
    assert state["plan"] == plan
    assert state["precheck_plan"] == precheck_plan
    assert state["precheck_enabled"] is precheck
    assert state["secretaria_enabled"] is secretaria
    expected = _quota(quota_plan) if quota_plan else 0
    assert state["limits"][catalog.LIMIT_PRECHECK_CONSULTATIONS] == expected


@pytest.mark.parametrize("row", [3, 4, 5, 7, 8])
def test_item_order_never_decides_the_product(row):
    items = next(m[1] for m in MATRIX if m[0] == row)
    forward = billing_service._state_from_subscription(make_sub(*items))
    backward = billing_service._state_from_subscription(make_sub(*reversed(items)))
    assert forward == backward


def test_row9_addon_quantity_scales_professionals_on_a_dual_subscription():
    sub = make_sub("price_pc_basic", *SEC_PRICES, ("price_multipro", 3))
    state = billing_service._state_from_subscription(sub)
    assert state["plan"] == "secretaria_basico"
    assert state["precheck_plan"] == "precheck_basic"
    # secretarIA base 1 + one add-on grant + (3 - 1) extra units
    assert state["limits"][catalog.LIMIT_PROFESSIONALS] == 4
    assert state["limits"][catalog.LIMIT_PRECHECK_CONSULTATIONS] == _quota("precheck_basic")


def test_row10_only_addons_is_not_a_recognized_plan():
    assert billing_service._state_from_subscription(make_sub(("price_multipro", 2))) is None
    assert billing_service._families_from_subscription(make_sub(("price_multipro", 2))) is None


def test_one_metered_companion_is_enough_evidence_of_secretaria():
    families = billing_service._families_from_subscription(make_sub("price_sec_rem"))
    assert families is not None
    assert families.secretaria_plan == "secretaria_basico"
    assert families.carried == {"secretaria"}


def test_families_carried_by_each_shape():
    def carried(*items):
        return billing_service._families_from_subscription(make_sub(*items)).carried

    assert carried("price_pc_start") == {"precheck"}
    assert carried(*SEC_PRICES) == {"secretaria"}
    assert carried(*SEC_PRICES, "price_pc_adv") == {"secretaria", "precheck"}
    assert carried("price_combo") == {"secretaria", "precheck"}


def test_single_family_subscriptions_match_the_legacy_derivation():
    for items, plan in (
        (["price_pc_basic"], "precheck_basic"),
        ([*SEC_PRICES], "secretaria_basico"),
        (["price_combo"], "complete_clinic_combo"),
    ):
        state = billing_service._state_from_subscription(make_sub(*items))
        legacy = catalog.compute_entitlement_state(plan)
        for key in ("precheck_enabled", "secretaria_enabled", "addons", "limits"):
            assert state[key] == legacy[key], (plan, key)


def test_limits_always_carry_the_full_keyset():
    for _row, items, *_ in MATRIX:
        state = billing_service._state_from_subscription(make_sub(*items))
        assert set(state["limits"]) == set(catalog.LIMIT_KEYS)
