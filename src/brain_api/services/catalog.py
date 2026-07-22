"""Product catalog — the single source of truth for plans, tiers, add-ons and limits.

Everything commercial is declared HERE and only here: plan/add-on ids, display names,
which products each plan enables (precheck/secretaria), which `entitlements.addons` /
`entitlements.limits` keys it sets, and the (placeholder) Stripe price ids. No price or
plan flag is hardcoded anywhere else — admin PATCH materializes entitlement rows FROM
this catalog today, and the Stripe webhook recompute (billing round) will reuse the same
`compute_*` helpers so both write paths stay consistent.

Per stripe-billing-entitlements: the catalog feeds the LOCAL `entitlements` row; runtime
gates read that row via `services.entitlements.is_entitled` — never Stripe, never this
module's prices.
"""

from dataclasses import dataclass, field

# --- secretarIA tier (single value since the 2026-07-22 tier-ladder retirement) -------
#
# ferro/bronze_1/bronze_2 used to be separate sellable plans; only ferro's fully-metered
# pricing model survived (as PLAN_SECRETARIA_BASICO below) and there is now exactly ONE
# secretarIA plan. `TIER_BASICO` / `SECRETARIA_TIERS` stay as a single-element sequence
# (rather than deleting the concept outright) purely so `is_entitled`'s generic
# "is this tenant on an active secretarIA subscription" tier check keeps working
# unchanged — nothing gates on more than one tier value anymore.

TIER_BASICO = "basico"

#: Ascending order; `is_entitled(ent, tier)` passes when the plan's tier ranks >= asked.
SECRETARIA_TIERS: tuple[str, ...] = (TIER_BASICO,)

# --- add-on ids (the formal `entitlements.addons` keys — always booleans) -------------

ADDON_REACTIVATION_PACK = "reactivation_pack"
ADDON_VERIFIED_IDENTITY = "verified_identity"
ADDON_MULTI_PROFESSIONAL = "multi_professional"
ADDON_MULTI_UNIT = "multi_unit"
ADDON_EHR = "ehr"
ADDON_PIX_DEPOSIT = "pix_deposit"
ADDON_ANALYTICS_BI = "analytics_bi"
#: Advanced BI dashboard — a SEPARATE, fixed-price add-on layered on top of the basic
#: `analytics_bi` summary. A DISTINCT entitlement key: owning one does not imply the other.
#: No `limit_grants` — value and cost are fixed per clinic, so it bills as ONE flat Stripe
#: line item (Camada 2) with no meter and no per-professional scaling.
ADDON_ANALYTICS_BI_ADVANCED = "analytics_bi_advanced"
ADDON_HUMAN_BACKUP_24_7 = "human_backup_24_7"

# --- limit keys (the formal `entitlements.limits` keys — always non-negative ints) ----

LIMIT_PROFESSIONALS = "professionals"  # professionals bookable on the calendar
LIMIT_UNITS = "units"  # clinic branches
LIMIT_MESSAGES = "messages"  # inbound conversations handled / month
#: Monthly METERED count of 24h/1h reminder sends that fall OUTSIDE the WhatsApp 24h
#: customer-service window (billable HSM template sends; free-form sends inside the
#: window don't count). Metering-only, same pattern as LIMIT_BILLABLE_PATIENTS /
#: LIMIT_ACTIVE_PROFESSIONALS below: no plan grants a base limit for it (stays 0 =
#: unlimited-by-quota) — it exists purely so POST /internal/usage-events validates
#: feature="reminders" (secretarIA's reminders plugin — a CORE capability, gated on
#: subscription activity, not this limit — is the caller) and services/usage.py's
#: Stripe meter-event forward (STRIPE_METER_EVENT_REMINDERS) has a validated key. The
#: third leg of the fully-metered secretaria_basico model, alongside billable_patients
#: and active_professionals (added 2026-07-22, retiring the old bronze_1 flat-fee
#: 200/month reminders quota, which was already unenforced in code).
LIMIT_REMINDERS = "reminders"
LIMIT_HSM_PROACTIVE = "hsm_proactive"  # proactive/reactivation HSM sends / month
#: Distinct (professional, patient) pairs billed this month (CONTRACT_onboarding_v1.md
#: §9, billing Phase 1). No plan grants a base limit for it (stays 0 = unlimited-by-quota
#: on every plan) — it exists purely so POST /internal/usage-events validates the
#: feature id and the ledger/Stripe-meter-forward path (services/usage.py) has a home.
LIMIT_BILLABLE_PATIENTS = "billable_patients"
#: Monthly METERED headcount Stripe bills under the fully-metered secretaria_basico model
#: (R$80/active professional/month). Metering-only, same pattern as LIMIT_BILLABLE_PATIENTS
#: above: no plan grants a base limit for it (stays 0 = unlimited-by-quota) — it exists
#: purely so POST /internal/usage-events validates feature="active_professionals" (fed by
#: secretarIA's own cron, a sibling change in that repo) and services/usage.py's Stripe
#: meter-event forward has a validated key. NOT the same concern as LIMIT_PROFESSIONALS
#: above: that one is the QUOTA ("professionals" = how many may be configured on the
#: calendar, a plan/add-on grant); this one is the BILLED COUNT ("active_professionals" =
#: how many were active this month, what Stripe actually charges for).
LIMIT_ACTIVE_PROFESSIONALS = "active_professionals"

LIMIT_KEYS: frozenset[str] = frozenset(
    (
        LIMIT_PROFESSIONALS,
        LIMIT_UNITS,
        LIMIT_MESSAGES,
        LIMIT_REMINDERS,
        LIMIT_HSM_PROACTIVE,
        LIMIT_BILLABLE_PATIENTS,
        LIMIT_ACTIVE_PROFESSIONALS,
    )
)

# --- plan ids --------------------------------------------------------------------------

PLAN_FREE = "free"
PLAN_PRECHECK = "precheck"
PLAN_SECRETARIA_BASICO = "secretaria_basico"
PLAN_COMPLETE_CLINIC_COMBO = "complete_clinic_combo"

#: Plan strings written before a catalog change, resolved to their current catalog plan
#: so already-provisioned rows (e.g. the seeded demo clinic's "brain-completo") keep
#: their add-on semantics without a data migration. "secretaria_ferro" covers the
#: 2026-07-22 tier-ladder retirement (ferro/bronze_1/bronze_2 -> one fully-metered
#: PLAN_SECRETARIA_BASICO) — ferro's pricing model (no flat/anchor price, metered
#: companions) carries forward unchanged under the new id, so the alias is safe.
#: bronze_1's flat-fee shape does NOT carry forward (retired outright — confirmed no
#: live subscribers to preserve) and is deliberately NOT aliased.
LEGACY_PLAN_ALIASES: dict[str, str] = {
    "brain-completo": PLAN_COMPLETE_CLINIC_COMBO,
    "secretaria_ferro": PLAN_SECRETARIA_BASICO,
}


@dataclass(frozen=True)
class AddonDef:
    """One purchasable add-on: an `entitlements.addons` flag + the limits it grants.

    `limit_grants` are ADDITIVE deltas applied on top of the plan's base limits when the
    add-on is active (per unit; Stripe item quantities scale them in the billing round).
    `stripe_price_id` is a placeholder until the billing round wires real prices.
    """

    id: str
    name: str
    description: str
    limit_grants: dict[str, int] = field(default_factory=dict)
    stripe_price_id: str | None = None  # TODO(billing round): real Stripe price id


@dataclass(frozen=True)
class PlanDef:
    """One sellable plan: products it enables, its secretarIA tier, implied add-ons.

    `included_addons` are add-ons the plan turns ON by itself (the combo models its
    bundled add-ons here — a combo is a plan that implies add-ons, not loose items).
    `available=False` marks a reserved slot that cannot be assigned yet.
    `discount_pct` is commercial metadata (the real discounted price lives in Stripe).
    """

    id: str
    name: str
    precheck: bool
    secretaria: bool
    secretaria_tier: str | None
    included_addons: tuple[str, ...] = ()
    base_limits: dict[str, int] = field(default_factory=dict)
    available: bool = True
    discount_pct: int = 0
    stripe_price_id: str | None = None  # TODO(billing round): real Stripe price id


ADDONS: dict[str, AddonDef] = {
    a.id: a
    for a in (
        AddonDef(
            id=ADDON_REACTIVATION_PACK,
            name="Reactivation Pack",
            description=(
                "Proactive reactivation of inactive patients via HSM templates outside "
                "the 24h window. (24h/1h appointment reminders are a core capability on "
                "every plan, billed per-send via the reminders Stripe meter — not part "
                "of this add-on.)"
            ),
            limit_grants={LIMIT_HSM_PROACTIVE: 200},
        ),
        AddonDef(
            id=ADDON_VERIFIED_IDENTITY,
            name="Verified Identity",
            description="Meta Verified for Business on the clinic's WhatsApp number.",
        ),
        AddonDef(
            id=ADDON_MULTI_PROFESSIONAL,
            name="Multi-professional",
            description=(
                "More than one doctor/dentist on the same calendar; grants one extra "
                "professional per unit purchased."
            ),
            limit_grants={LIMIT_PROFESSIONALS: 1},
        ),
        AddonDef(
            id=ADDON_MULTI_UNIT,
            name="Multi-unit",
            description="Clinics with branches; grants one extra unit per unit purchased.",
            limit_grants={LIMIT_UNITS: 1},
        ),
        AddonDef(
            id=ADDON_EHR,
            name="EHR integration",
            description=(
                "Electronic health record integration (iClinic, Doctoralia, Memed, Conexa)."
            ),
        ),
        AddonDef(
            id=ADDON_PIX_DEPOSIT,
            name="Pix deposit",
            description=(
                "Appointment deposit (sinal) via Pix on WhatsApp: dynamic QR charge at "
                "booking, refund-window policy, retention on late cancel/no-show. Fixed "
                "price per clinic."
            ),
            # Intentionally no limit_grants: a flat Stripe line item, not scaled per
            # unit — the PSP fee (~R$1.99 at Asaas) is paid by the CLINIC on its OWN
            # PSP account, so it is not our cost and is not metered.
            # Renamed from `pix_whatsapp` to `pix_deposit` (2026-07-21, pre-launch,
            # no live subscribers — no data migration needed).
        ),
        AddonDef(
            id=ADDON_ANALYTICS_BI,
            name="Analytics / BI",
            description="BI dashboard of the secretary's operation.",
        ),
        AddonDef(
            id=ADDON_ANALYTICS_BI_ADVANCED,
            name="Advanced Dashboard",
            description=(
                "Advanced BI dashboard: per-professional and per-source booking "
                "breakdowns plus longer-range trends, layered on top of the basic "
                "Analytics/BI summary. Fixed price per clinic (no per-professional "
                "scaling)."
            ),
            # Intentionally NO limit_grants: a fixed-value, fixed-cost add-on that bills
            # as one flat Stripe line item — nothing to scale per unit.
        ),
        AddonDef(
            id=ADDON_HUMAN_BACKUP_24_7,
            name="Human backup 24/7",
            description="Human fallback support outside business hours.",
        ),
    )
}

#: The formal `entitlements.addons` keyset. Every materialized row carries ALL of these.
ADDON_IDS: frozenset[str] = frozenset(ADDONS)

PLANS: dict[str, PlanDef] = {
    p.id: p
    for p in (
        PlanDef(
            id=PLAN_FREE,
            name="Free",
            precheck=False,
            secretaria=False,
            secretaria_tier=None,
        ),
        PlanDef(
            id=PLAN_PRECHECK,
            name="PreCheck",
            precheck=True,
            secretaria=False,
            secretaria_tier=None,
        ),
        PlanDef(
            id=PLAN_SECRETARIA_BASICO,
            name="secretarIA Básico",
            precheck=False,
            secretaria=True,
            secretaria_tier=TIER_BASICO,
            # Fully metered, NO flat/anchor price: three companion Stripe Meters bill
            # active professionals, billable patients, and reminders sent outside the
            # 24h window (billing.py's METERED_*_SUFFIX / STRIPE_PRICE_MAP companions).
            # Replaces the old ferro/bronze_1/bronze_2 tier ladder (2026-07-22) — every
            # secretarIA tenant is on this ONE plan; analytics_bi_advanced and
            # pix_deposit are optional flat add-ons layered on top, not separate tiers.
            base_limits={LIMIT_PROFESSIONALS: 1, LIMIT_UNITS: 1, LIMIT_MESSAGES: 400},
        ),
        PlanDef(
            id=PLAN_COMPLETE_CLINIC_COMBO,
            name="Complete Clinic Combo",
            precheck=True,
            secretaria=True,
            secretaria_tier=TIER_BASICO,
            included_addons=(ADDON_REACTIVATION_PACK, ADDON_VERIFIED_IDENTITY),
            base_limits={LIMIT_PROFESSIONALS: 1, LIMIT_UNITS: 1, LIMIT_MESSAGES: 400},
            # ~15% off the sum of the parts; the actual discounted amount lives on the
            # Stripe price (billing round), this is display/derivation metadata only.
            discount_pct=15,
        ),
    )
}

PLAN_IDS: frozenset[str] = frozenset(PLANS)

#: Plan ids an admin (or, later, a checkout) may assign — excludes reserved slots.
ASSIGNABLE_PLAN_IDS: frozenset[str] = frozenset(p.id for p in PLANS.values() if p.available)


def get_plan(plan_id: str | None) -> PlanDef | None:
    """Resolve a plan id (or legacy alias) to its definition; None when unknown.

    Unknown plans resolve to None so runtime gates fail CLOSED (no tier, no implied
    add-ons) instead of crashing on a stale row.
    """
    if plan_id is None:
        return None
    return PLANS.get(LEGACY_PLAN_ALIASES.get(plan_id, plan_id))


def get_addon(addon_id: str) -> AddonDef | None:
    """Resolve an add-on id to its definition; None when unknown."""
    return ADDONS.get(addon_id)


def tier_rank(tier: str | None) -> int:
    """Position of a tier in the cumulative order; -1 for None/unknown (ranks below all)."""
    try:
        return SECRETARIA_TIERS.index(tier) if tier is not None else -1
    except ValueError:
        return -1


def plan_tier(plan_id: str | None) -> str | None:
    """The secretarIA tier a plan grants (None for unknown plans / plans without one)."""
    plan = get_plan(plan_id)
    return plan.secretaria_tier if plan is not None else None


def default_addons(plan_id: str | None) -> dict[str, bool]:
    """The full, formalized `addons` object for a plan: every add-on id, plan-implied ON.

    Materialized rows always carry the complete keyset so consumers never need
    `.get(key, False)` guesswork about missing keys.
    """
    plan = get_plan(plan_id)
    included = set(plan.included_addons) if plan is not None else set()
    return {addon_id: addon_id in included for addon_id in ADDONS}


def compute_limits(plan_id: str | None, addons: dict[str, bool] | None = None) -> dict[str, int]:
    """The effective `limits` object: plan base limits + additive grants of active add-ons.

    Always emits the complete `LIMIT_KEYS` set (absent = 0) so the row shape is stable.
    Add-on grants are additive per unit; Stripe item quantities will scale them in the
    billing round.
    """
    plan = get_plan(plan_id)
    limits = {key: 0 for key in sorted(LIMIT_KEYS)}
    if plan is not None:
        limits.update(plan.base_limits)
    for addon_id, active in (addons or {}).items():
        addon = ADDONS.get(addon_id)
        if active and addon is not None:
            for key, grant in addon.limit_grants.items():
                limits[key] = limits.get(key, 0) + grant
    return limits


def compute_entitlement_state(
    plan_id: str,
    addon_overrides: dict[str, bool] | None = None,
) -> dict:
    """Materialize the full entitlement state a plan (+ optional add-on choices) implies.

    The one derivation both write paths share: admin PATCH uses it today, the Stripe
    webhook recompute will use it in the billing round. Returns the column values to
    write: `precheck_enabled`, `secretaria_enabled`, `addons` (full keyset, plan-implied
    add-ons ON, overrides applied on top) and `limits` (recomputed from the result).
    Raises `ValueError` for a plan id that is not in the catalog.
    """
    plan = get_plan(plan_id)
    if plan is None:
        raise ValueError(f"unknown_plan:{plan_id}")
    addons = default_addons(plan.id)
    for addon_id, active in (addon_overrides or {}).items():
        if addon_id in addons:
            addons[addon_id] = bool(active)
    return {
        "precheck_enabled": plan.precheck,
        "secretaria_enabled": plan.secretaria,
        "addons": addons,
        "limits": compute_limits(plan.id, addons),
    }
