"""catalog.compose_entitlement_state + the per-family readers (TASK C, spec §5.1)."""

from types import SimpleNamespace

import pytest

from brain_api.services import catalog


def _quota(plan_id: str) -> int:
    return catalog.get_plan(plan_id).base_limits[catalog.LIMIT_PRECHECK_CONSULTATIONS]


@pytest.mark.parametrize(
    ("secretaria_plan", "precheck_plan"),
    [
        (None, catalog.PLAN_PRECHECK_START),
        (None, catalog.PLAN_PRECHECK_BASIC),
        (None, catalog.PLAN_PRECHECK_ADVANCED),
        (catalog.PLAN_SECRETARIA_BASICO, None),
        (catalog.PLAN_COMPLETE_CLINIC_COMBO, None),
        (None, None),
    ],
)
def test_single_family_matches_the_legacy_derivation(secretaria_plan, precheck_plan):
    """Zero regression for one-family clinics: flags/addons/limits are what
    compute_entitlement_state has always produced."""
    anchor = secretaria_plan or precheck_plan or catalog.PLAN_FREE
    legacy = catalog.compute_entitlement_state(anchor)
    state = catalog.compose_entitlement_state(secretaria_plan, precheck_plan)
    assert state["plan"] == anchor
    assert state["precheck_plan"] is None
    for key in ("precheck_enabled", "secretaria_enabled", "addons", "limits"):
        assert state[key] == legacy[key]


def test_dual_clinic_anchors_on_secretaria_and_keeps_the_precheck_tier():
    state = catalog.compose_entitlement_state(
        catalog.PLAN_SECRETARIA_BASICO, catalog.PLAN_PRECHECK_BASIC
    )
    assert state["plan"] == catalog.PLAN_SECRETARIA_BASICO
    assert state["precheck_plan"] == catalog.PLAN_PRECHECK_BASIC
    assert state["precheck_enabled"] is True
    assert state["secretaria_enabled"] is True
    assert state["limits"][catalog.LIMIT_PRECHECK_CONSULTATIONS] == _quota(
        catalog.PLAN_PRECHECK_BASIC
    )
    # the secretarIA limits are still the secretarIA plan's
    assert state["limits"][catalog.LIMIT_PROFESSIONALS] == 1
    assert state["limits"][catalog.LIMIT_MESSAGES] == 400


def test_combo_wins_over_a_precheck_tier():
    state = catalog.compose_entitlement_state(
        catalog.PLAN_COMPLETE_CLINIC_COMBO, catalog.PLAN_PRECHECK_START
    )
    assert state["plan"] == catalog.PLAN_COMPLETE_CLINIC_COMBO
    assert state["precheck_plan"] is None
    assert state["precheck_enabled"] is True
    assert state["limits"][catalog.LIMIT_PRECHECK_CONSULTATIONS] == _quota(
        catalog.PLAN_COMPLETE_CLINIC_COMBO
    )


def test_limits_always_carry_the_full_keyset_and_compose_is_idempotent():
    overrides = {catalog.ADDON_MULTI_PROFESSIONAL: True, "not_an_addon": True}
    first = catalog.compose_entitlement_state(
        catalog.PLAN_SECRETARIA_BASICO, catalog.PLAN_PRECHECK_ADVANCED, overrides
    )
    second = catalog.compose_entitlement_state(
        catalog.PLAN_SECRETARIA_BASICO, catalog.PLAN_PRECHECK_ADVANCED, overrides
    )
    assert first == second
    assert set(first["limits"]) == set(catalog.LIMIT_KEYS)
    assert first["addons"][catalog.ADDON_MULTI_PROFESSIONAL] is True
    assert "not_an_addon" not in first["addons"]
    assert first["limits"][catalog.LIMIT_PROFESSIONALS] == 2  # base 1 + one multi_professional


@pytest.mark.parametrize(
    ("secretaria_plan", "precheck_plan"),
    [
        (catalog.PLAN_PRECHECK_BASIC, None),  # not a secretarIA plan
        (None, catalog.PLAN_SECRETARIA_BASICO),  # not a PreCheck tier
        (None, catalog.PLAN_COMPLETE_CLINIC_COMBO),  # the combo is not a tier
        ("nope", None),
        (None, "nope"),
    ],
)
def test_compose_rejects_a_plan_in_the_wrong_slot(secretaria_plan, precheck_plan):
    with pytest.raises(ValueError):
        catalog.compose_entitlement_state(secretaria_plan, precheck_plan)


def _ent(**fields) -> SimpleNamespace:
    base = dict(plan="free", precheck_plan=None, precheck_enabled=False, secretaria_enabled=False)
    base.update(fields)
    return SimpleNamespace(**base)


def test_precheck_plan_of_covers_every_row_shape():
    assert catalog.precheck_plan_of(None) is None
    assert catalog.precheck_plan_of(_ent()) is None
    assert catalog.precheck_plan_of(_ent(plan=catalog.PLAN_SECRETARIA_BASICO)) is None
    only = catalog.precheck_plan_of(_ent(plan=catalog.PLAN_PRECHECK_START))
    assert only.id == catalog.PLAN_PRECHECK_START
    dual = catalog.precheck_plan_of(
        _ent(plan=catalog.PLAN_SECRETARIA_BASICO, precheck_plan=catalog.PLAN_PRECHECK_ADVANCED)
    )
    assert dual.id == catalog.PLAN_PRECHECK_ADVANCED
    combo = catalog.precheck_plan_of(_ent(plan=catalog.PLAN_COMPLETE_CLINIC_COMBO))
    assert combo.id == catalog.PLAN_COMPLETE_CLINIC_COMBO
    # legacy alias resolves to Basic
    assert catalog.precheck_plan_of(_ent(plan="precheck")).id == catalog.PLAN_PRECHECK_BASIC


def test_secretaria_plan_of_and_families_enabled():
    assert catalog.secretaria_plan_of(_ent(plan=catalog.PLAN_PRECHECK_BASIC)) is None
    assert catalog.secretaria_plan_of(_ent(plan=catalog.PLAN_SECRETARIA_BASICO)).secretaria
    assert catalog.secretaria_plan_of(_ent(plan=catalog.PLAN_COMPLETE_CLINIC_COMBO)).secretaria
    assert catalog.families_enabled(_ent()) == []
    assert catalog.families_enabled(_ent(precheck_enabled=True)) == ["precheck"]
    assert catalog.families_enabled(_ent(secretaria_enabled=True, precheck_enabled=True)) == [
        "precheck",
        "secretaria",
    ]
