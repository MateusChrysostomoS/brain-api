"""Catalog + entitlements coverage for the plan/add-on catalog round.

Four layers, cheapest first:
- Catalog integrity (`services/catalog.py`): every declared id/limit key is internally
  consistent (no reserved/unassignable slots remain since the 2026-07-22 tier-ladder
  retirement — every catalog plan is assignable).
- The pure derivation helpers (`compute_limits`, `default_addons`,
  `compute_entitlement_state`): the ONE place both the admin PATCH and the future Stripe
  webhook recompute share.
- `is_entitled` (`services/entitlements.py`): the runtime yes/no gate, exercised against
  lightweight `EntitlementLike` stand-ins (no DB needed — it only reads plan/status/addons).
- The HTTP surface (`GET /entitlements`, admin `PATCH .../entitlements`) against the real
  app + in-memory DB, reusing the seeded `client` fixture from `tests/test_rbac.py`.
"""

from types import SimpleNamespace

import pytest

from brain_api.services import catalog
from brain_api.services.entitlements import is_entitled
from tests.test_rbac import (
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    CLINIC_B,
    OWNER_A_EMAIL,
    OWNER_A_PASSWORD,
    OWNER_B_EMAIL,
    OWNER_B_PASSWORD,
    _bearer,
    _token,
)


def _ent(plan: str, status: str = "active", addons: dict | None = None) -> SimpleNamespace:
    """A lightweight `EntitlementLike` stand-in — `is_entitled` only reads these 3 fields."""
    return SimpleNamespace(plan=plan, status=status, addons=addons if addons is not None else {})


# ---------------------------------------------------------------------------
# 1) Catalog integrity (pure, no client)
# ---------------------------------------------------------------------------


def test_plan_included_addons_and_base_limits_are_known_ids():
    for plan in catalog.PLANS.values():
        assert set(plan.included_addons) <= catalog.ADDON_IDS, plan.id
        assert set(plan.base_limits) <= catalog.LIMIT_KEYS, plan.id


def test_addon_limit_grants_are_known_limit_keys():
    for addon in catalog.ADDONS.values():
        assert set(addon.limit_grants) <= catalog.LIMIT_KEYS, addon.id


def test_plan_secretaria_tier_valid_and_implies_secretaria_flag():
    for plan in catalog.PLANS.values():
        assert plan.secretaria_tier is None or plan.secretaria_tier in catalog.SECRETARIA_TIERS
        if plan.secretaria_tier is not None:
            assert plan.secretaria is True, plan.id


def test_assignable_plan_ids_has_no_reserved_slots():
    """The reserved-slot concept (`available=False`, formerly excluding
    secretaria_bronze_2) is gone entirely as of the 2026-07-22 tier-ladder retirement —
    every catalog plan is currently assignable. PLAN_PRECHECK ("precheck") is no longer a
    `PLANS` member as of the 2026-08-01 PreCheck-billing split — it lives on only as a
    LEGACY_PLAN_ALIASES key (see test_get_plan_resolves_precheck_alias_to_basic below),
    replaced here by the three real tiers (Start joined 2026-09-03)."""
    assert catalog.ASSIGNABLE_PLAN_IDS == catalog.PLAN_IDS
    assert catalog.ASSIGNABLE_PLAN_IDS == frozenset(
        {
            catalog.PLAN_FREE,
            catalog.PLAN_PRECHECK_START,
            catalog.PLAN_PRECHECK_BASIC,
            catalog.PLAN_PRECHECK_ADVANCED,
            catalog.PLAN_SECRETARIA_BASICO,
            catalog.PLAN_COMPLETE_CLINIC_COMBO,
        }
    )


def test_get_plan_resolves_legacy_alias_and_fails_closed():
    assert catalog.get_plan("brain-completo").id == catalog.PLAN_COMPLETE_CLINIC_COMBO
    assert catalog.get_plan("nonsense") is None
    assert catalog.get_plan(None) is None


def test_get_plan_resolves_precheck_alias_to_basic():
    """The legacy bare "precheck" id (pre-2026-08-01 PreCheck-billing split) resolves to
    the new PreCheck Basic tier — protects already-seeded/demo rows (e.g. the client
    fixture's Clinic A, plan="precheck") without a data migration."""
    plan = catalog.get_plan(catalog.PLAN_PRECHECK)
    assert plan is not None
    assert plan.id == catalog.PLAN_PRECHECK_BASIC
    assert plan.precheck is True
    assert plan.secretaria is False


def test_precheck_plans_carry_env_default_quotas():
    """Each PreCheck tier's LIMIT_PRECHECK_CONSULTATIONS base_limit is read from Settings
    (PRECHECK_START_/BASIC_/ADVANCED_CONSULTATIONS_PER_MONTH) at catalog import time;
    hermetic tests set none of the env vars, so the code defaults (50 / 100 / 300)
    apply."""
    from brain_api.config import get_settings

    settings = get_settings()
    start = catalog.get_plan(catalog.PLAN_PRECHECK_START)
    basic = catalog.get_plan(catalog.PLAN_PRECHECK_BASIC)
    advanced = catalog.get_plan(catalog.PLAN_PRECHECK_ADVANCED)
    assert start.base_limits[catalog.LIMIT_PRECHECK_CONSULTATIONS] == (
        settings.PRECHECK_START_CONSULTATIONS_PER_MONTH
    )
    assert basic.base_limits[catalog.LIMIT_PRECHECK_CONSULTATIONS] == (
        settings.PRECHECK_BASIC_CONSULTATIONS_PER_MONTH
    )
    assert advanced.base_limits[catalog.LIMIT_PRECHECK_CONSULTATIONS] == (
        settings.PRECHECK_ADVANCED_CONSULTATIONS_PER_MONTH
    )
    # Env defaults, asserted as literals too so a silent default change is caught here.
    assert settings.PRECHECK_START_CONSULTATIONS_PER_MONTH == 50
    assert settings.PRECHECK_BASIC_CONSULTATIONS_PER_MONTH == 100
    assert settings.PRECHECK_ADVANCED_CONSULTATIONS_PER_MONTH == 300


def test_precheck_tier_ladder_is_ordered_and_excludes_the_combo():
    """PRECHECK_TIER_PLAN_IDS is what POST /billing/precheck/upgrade accepts as a target
    (services/billing.py::upgrade_precheck_plan). It must list the three standalone tiers
    CHEAPEST FIRST (the frontend renders the ladder in this order) and must NOT include
    the combo: the combo is precheck=True, so a membership test written against
    `PlanDef.precheck` instead of this tuple would let a combo tenant swap into a
    PreCheck-only plan and silently lose secretarIA."""
    assert catalog.PRECHECK_TIER_PLAN_IDS == (
        catalog.PLAN_PRECHECK_START,
        catalog.PLAN_PRECHECK_BASIC,
        catalog.PLAN_PRECHECK_ADVANCED,
    )
    assert catalog.PLAN_COMPLETE_CLINIC_COMBO not in catalog.PRECHECK_TIER_PLAN_IDS
    assert catalog.get_plan(catalog.PLAN_COMPLETE_CLINIC_COMBO).precheck is True

    quotas = [
        catalog.get_plan(pid).base_limits[catalog.LIMIT_PRECHECK_CONSULTATIONS]
        for pid in catalog.PRECHECK_TIER_PLAN_IDS
    ]
    assert quotas == sorted(quotas), quotas
    assert all(catalog.get_plan(pid).precheck for pid in catalog.PRECHECK_TIER_PLAN_IDS)


def test_combo_carries_the_advanced_precheck_quota():
    """The premium combo (precheck=True) must not leave PreCheck unenforced (limit 0) —
    it carries the SAME quota as the standalone Advanced tier."""
    from brain_api.config import get_settings

    settings = get_settings()
    combo = catalog.get_plan(catalog.PLAN_COMPLETE_CLINIC_COMBO)
    assert combo.base_limits[catalog.LIMIT_PRECHECK_CONSULTATIONS] == (
        settings.PRECHECK_ADVANCED_CONSULTATIONS_PER_MONTH
    )
    limits = catalog.compute_limits(catalog.PLAN_COMPLETE_CLINIC_COMBO)
    assert limits[catalog.LIMIT_PRECHECK_CONSULTATIONS] == (
        settings.PRECHECK_ADVANCED_CONSULTATIONS_PER_MONTH
    )


# ---------------------------------------------------------------------------
# 2) compute_limits / default_addons / compute_entitlement_state (pure)
# ---------------------------------------------------------------------------


def test_compute_limits_always_returns_full_keyset():
    limits = catalog.compute_limits("nonsense")
    assert set(limits) == catalog.LIMIT_KEYS
    assert all(v == 0 for v in limits.values())


def test_compute_limits_addon_grants_are_additive():
    limits = catalog.compute_limits(
        catalog.PLAN_SECRETARIA_BASICO, {catalog.ADDON_MULTI_UNIT: True}
    )
    # base units=1 + multi_unit grant=1
    assert limits[catalog.LIMIT_UNITS] == 2


def test_default_addons_combo_has_full_keyset_with_bundled_addons_on():
    addons = catalog.default_addons(catalog.PLAN_COMPLETE_CLINIC_COMBO)
    assert set(addons) == catalog.ADDON_IDS
    assert addons[catalog.ADDON_REACTIVATION_PACK] is True
    assert addons[catalog.ADDON_VERIFIED_IDENTITY] is True
    for addon_id in catalog.ADDON_IDS - {
        catalog.ADDON_REACTIVATION_PACK,
        catalog.ADDON_VERIFIED_IDENTITY,
    }:
        assert addons[addon_id] is False


def test_compute_entitlement_state_combo():
    state = catalog.compute_entitlement_state(catalog.PLAN_COMPLETE_CLINIC_COMBO)
    assert state["precheck_enabled"] is True
    assert state["secretaria_enabled"] is True
    assert state["addons"][catalog.ADDON_REACTIVATION_PACK] is True
    assert state["addons"][catalog.ADDON_VERIFIED_IDENTITY] is True
    assert state["limits"][catalog.LIMIT_HSM_PROACTIVE] == 200
    # reminders is metering-only now (2026-07-22) -- no plan/add-on grants a base quota.
    assert state["limits"][catalog.LIMIT_REMINDERS] == 0


def test_compute_entitlement_state_addon_override_grants_limit():
    state = catalog.compute_entitlement_state(
        catalog.PLAN_SECRETARIA_BASICO, {catalog.ADDON_MULTI_PROFESSIONAL: True}
    )
    # basico base professionals=1 + multi_professional grant=1
    assert state["limits"][catalog.LIMIT_PROFESSIONALS] == 2


def test_compute_entitlement_state_unknown_plan_raises():
    with pytest.raises(ValueError):
        catalog.compute_entitlement_state("nonsense")


# ---------------------------------------------------------------------------
# 3) is_entitled (pure — lightweight EntitlementLike stand-ins)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["inactive", "past_due"])
def test_is_entitled_status_gate_denies_everything(status):
    ent = _ent(
        catalog.PLAN_COMPLETE_CLINIC_COMBO,
        status=status,
        addons={catalog.ADDON_REACTIVATION_PACK: True},
    )
    assert is_entitled(ent, catalog.ADDON_REACTIVATION_PACK) is False
    assert is_entitled(ent, catalog.TIER_BASICO) is False


def test_is_entitled_tier_true_for_the_single_secretaria_tier():
    """Only one secretarIA tier exists now (2026-07-22 retirement of the ferro/bronze_1/
    bronze_2 ladder) -- replaces the old "tiers are cumulative" test, which asserted
    behavior across multiple tiers that no longer exist. The rank check still passes for
    a plan carrying the (sole) tier it asks about."""
    ent = _ent(catalog.PLAN_SECRETARIA_BASICO, status="active")
    assert is_entitled(ent, catalog.TIER_BASICO) is True


def test_is_entitled_plan_without_tier_denies_basico():
    ent = _ent(catalog.PLAN_PRECHECK, status="active")
    assert is_entitled(ent, catalog.TIER_BASICO) is False


def test_is_entitled_addon_by_explicit_flag():
    on = _ent(catalog.PLAN_PRECHECK, status="active", addons={catalog.ADDON_EHR: True})
    off = _ent(catalog.PLAN_PRECHECK, status="active", addons={})
    assert is_entitled(on, catalog.ADDON_EHR) is True
    assert is_entitled(off, catalog.ADDON_EHR) is False


def test_is_entitled_addon_implied_by_plan_even_when_unmaterialized():
    ent = _ent(catalog.PLAN_COMPLETE_CLINIC_COMBO, status="active", addons={})
    assert is_entitled(ent, catalog.ADDON_REACTIVATION_PACK) is True


def test_is_entitled_legacy_alias_row_resolves_tier_and_addons():
    ent = _ent("brain-completo", status="active", addons={})
    assert is_entitled(ent, catalog.ADDON_VERIFIED_IDENTITY) is True
    assert is_entitled(ent, catalog.TIER_BASICO) is True


def test_is_entitled_unknown_key_raises():
    ent = _ent(catalog.PLAN_PRECHECK, status="active")
    with pytest.raises(ValueError):
        is_entitled(ent, "bogus_key")


# ---------------------------------------------------------------------------
# 4) GET /entitlements through the API
# ---------------------------------------------------------------------------


async def test_get_entitlements_owner_a_precheck_plan(client):
    """Clinic A is seeded with the LEGACY plan string "precheck" (test_rbac.py), which
    resolves through LEGACY_PLAN_ALIASES to PLAN_PRECHECK_BASIC. Since the 2026-08-01
    PreCheck-billing split, that plan carries a real (env-default) LIMIT_PRECHECK_
    CONSULTATIONS quota — every OTHER limit key still reads 0."""
    from brain_api.config import get_settings

    token = await _token(client, OWNER_A_EMAIL, OWNER_A_PASSWORD)
    resp = await client.get("/entitlements", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan"] == catalog.PLAN_PRECHECK
    assert body["secretaria_tier"] is None
    assert set(body["addons"]) == catalog.ADDON_IDS
    assert all(v is False for v in body["addons"].values())
    assert set(body["limits"]) == catalog.LIMIT_KEYS
    expected_quota = get_settings().PRECHECK_BASIC_CONSULTATIONS_PER_MONTH
    for key, value in body["limits"].items():
        if key == catalog.LIMIT_PRECHECK_CONSULTATIONS:
            assert value == expected_quota
        else:
            assert value == 0
    assert body["products"]["precheck"] is True


async def test_get_entitlements_owner_b_default_free(client):
    token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    resp = await client.get("/entitlements", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["plan"] == catalog.PLAN_FREE
    assert body["status"] == "inactive"
    assert set(body["addons"]) == catalog.ADDON_IDS
    assert all(v is False for v in body["addons"].values())
    assert body["secretaria_tier"] is None


# ---------------------------------------------------------------------------
# 5) Admin PATCH materialization
# ---------------------------------------------------------------------------


async def _tenant_b_id(client, admin_token: str) -> str:
    tenants = (await client.get("/admin/tenants", headers=_bearer(admin_token))).json()["items"]
    return next(t["id"] for t in tenants if t["clinic_name"] == CLINIC_B)


async def test_admin_patch_chain_plan_then_addons_then_limits(client):
    """Each PATCH layers on the previous state: plan -> addon override -> limit override."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = await _tenant_b_id(client, admin_token)

    # 1) Setting `plan` materializes products/addons/limits from the catalog.
    resp = await client.patch(
        f"/admin/tenants/{tenant_b_id}/entitlements",
        headers=_bearer(admin_token),
        json={"plan": catalog.PLAN_COMPLETE_CLINIC_COMBO, "status": "active"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["precheck_enabled"] is True
    assert body["secretaria_enabled"] is True
    assert body["addons"][catalog.ADDON_REACTIVATION_PACK] is True
    assert body["addons"][catalog.ADDON_VERIFIED_IDENTITY] is True
    # reminders is metering-only now (2026-07-22) -- no plan/add-on grants a base quota.
    assert body["limits"][catalog.LIMIT_REMINDERS] == 0

    owner_b_token = await _token(client, OWNER_B_EMAIL, OWNER_B_PASSWORD)
    ent = (await client.get("/entitlements", headers=_bearer(owner_b_token))).json()
    assert ent["products"]["precheck"] is True
    assert ent["products"]["secretaria"] is True
    assert ent["secretaria_tier"] == catalog.TIER_BASICO
    assert ent["addons"][catalog.ADDON_REACTIVATION_PACK] is True
    assert ent["addons"][catalog.ADDON_VERIFIED_IDENTITY] is True
    # Every non-bundled addon reads False, consistent with is_entitled's plan-implied check.
    for addon_id in catalog.ADDON_IDS - {
        catalog.ADDON_REACTIVATION_PACK,
        catalog.ADDON_VERIFIED_IDENTITY,
    }:
        assert ent["addons"][addon_id] is False

    # 2) A patched `addons` normalizes to the full keyset; the combo-implied addons stay
    # True (they come from the plan's own default, not from this patch).
    resp = await client.patch(
        f"/admin/tenants/{tenant_b_id}/entitlements",
        headers=_bearer(admin_token),
        json={"addons": {catalog.ADDON_MULTI_PROFESSIONAL: True}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body["addons"]) == catalog.ADDON_IDS
    assert body["addons"][catalog.ADDON_REACTIVATION_PACK] is True
    assert body["addons"][catalog.ADDON_VERIFIED_IDENTITY] is True
    assert body["addons"][catalog.ADDON_MULTI_PROFESSIONAL] is True
    assert body["limits"][catalog.LIMIT_PROFESSIONALS] == 2

    # 3) An explicit `limits` override merges on top of the recomputed limits — only the
    # named key changes, the rest stay at their recomputed values.
    resp = await client.patch(
        f"/admin/tenants/{tenant_b_id}/entitlements",
        headers=_bearer(admin_token),
        json={"limits": {catalog.LIMIT_MESSAGES: 999}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["limits"][catalog.LIMIT_MESSAGES] == 999
    assert body["limits"][catalog.LIMIT_PROFESSIONALS] == 2
    assert body["limits"][catalog.LIMIT_REMINDERS] == 0


async def test_admin_patch_rejects_unassignable_or_unknown_plan(client):
    """The "known but unassignable (reserved slot)" case (e.g. the old
    secretaria_bronze_2 reserved slot) no longer applies -- the reserved-slot concept was
    retired 2026-07-22 along with the tier ladder, so a genuinely unknown plan id is the
    only 422 case left to exercise here."""
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = await _tenant_b_id(client, admin_token)

    resp = await client.patch(
        f"/admin/tenants/{tenant_b_id}/entitlements",
        headers=_bearer(admin_token),
        json={"plan": "nonsense"},
    )
    assert resp.status_code == 422, resp.text


async def test_admin_patch_rejects_unknown_addon_and_bad_limits(client):
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = await _tenant_b_id(client, admin_token)

    for payload in (
        {"addons": {"bogus": True}},
        {"limits": {"bogus": 1}},
        {"limits": {catalog.LIMIT_MESSAGES: -1}},
    ):
        resp = await client.patch(
            f"/admin/tenants/{tenant_b_id}/entitlements",
            headers=_bearer(admin_token),
            json=payload,
        )
        assert resp.status_code == 422, f"{payload}: {resp.text}"


async def test_admin_patch_legacy_alias_normalizes_to_canonical_plan(client):
    admin_token = await _token(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    tenant_b_id = await _tenant_b_id(client, admin_token)

    resp = await client.patch(
        f"/admin/tenants/{tenant_b_id}/entitlements",
        headers=_bearer(admin_token),
        json={"plan": "brain-completo"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["plan"] == catalog.PLAN_COMPLETE_CLINIC_COMBO
