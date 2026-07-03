"""Entitlement resolution service (stripe-billing-entitlements skill, CONTRACTS.md §3.1).

The runtime authority for "what is this tenant allowed to use, right now". Resolved
synchronously, in-process, from the LOCAL `entitlements` row keyed by tenant_id.

NEVER call Stripe to answer a read: Stripe is a write-only money sink, recomputed INTO
our row by webhooks (a future round). The dashboard reads our counters (`ent.usage`),
not Stripe. This module performs a pure local DB read — no network, no Stripe SDK.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.models import Entitlement, Tenant
from brain_api.schemas.entitlement import EntitlementOut, ProductsOut
from brain_api.services import catalog

#: Subscription states under which any gate may pass (mirrors `check_quota`).
ACTIVE_STATUSES = ("active", "trialing")


class EntitlementLike(Protocol):
    """The three fields `is_entitled` reads — satisfied by both the `Entitlement` ORM row
    and the resolved `EntitlementOut`, so callers on either side of the API (brain-api
    gates now, secretarIA's plugin gates later) share one semantics."""

    plan: str
    status: str
    addons: dict


def is_entitled(ent: EntitlementLike, key: str) -> bool:
    """THE single yes/no gate: is this tenant allowed to use `key`, right now?

    `key` is either an add-on id (catalog `ADDON_IDS`) or a secretarIA tier
    (catalog `SECRETARIA_TIERS`). Pure and in-process — no network, no Stripe.

    Rules (fail-closed throughout):
    - status not active/trialing -> False, whatever was bought.
    - add-on -> ON in `ent.addons`, OR implied by the plan (a combo is a plan that
      implies add-ons — an unmaterialized row still answers correctly).
    - tier   -> the plan's tier ranks >= the asked tier (tiers are cumulative:
      bronze_1 includes everything ferro does). Unknown/legacy plans rank below all.
    - a `key` that is neither an add-on nor a tier is a programmer error -> ValueError
      (loud, so a typo'd gate id can't silently deny — or grant — forever).
    """
    if key in catalog.ADDON_IDS:
        if ent.status not in ACTIVE_STATUSES:
            return False
        if (ent.addons or {}).get(key) is True:
            return True
        plan = catalog.get_plan(ent.plan)
        return plan is not None and key in plan.included_addons
    if key in catalog.SECRETARIA_TIERS:
        if ent.status not in ACTIVE_STATUSES:
            return False
        return catalog.tier_rank(catalog.plan_tier(ent.plan)) >= catalog.tier_rank(key)
    raise ValueError(f"unknown_entitlement_key:{key}")


async def resolve_entitlement(session: AsyncSession, tenant_id: UUID) -> EntitlementOut:
    """Resolve the entitlement state for a tenant from the local DB.

    Loads the `Tenant` (for `clinic_name`) and the `Entitlement` row (PK = tenant_id).
    Pure local read — NO Stripe, NO network.

    Resolution rules (CONTRACTS.md §3.1):
    - No `entitlements` row for the tenant -> return a coherent DEFAULT (both products
      `False`, `plan="free"`, `status="inactive"`, empty scaffolds). Never 404 a valid
      tenant; the portal must always render a coherent state.
    - Tenant row missing (shouldn't happen for a valid token) -> `clinic_name=""` rather
      than crashing.
    """
    tenant = await session.get(Tenant, tenant_id)
    ent = await session.get(Entitlement, tenant_id)

    clinic_name = tenant.clinic_name if tenant is not None else ""

    if ent is None:
        # Default state for a tenant with no entitlement row yet.
        return EntitlementOut(
            tenant_id=tenant_id,
            clinic_name=clinic_name,
            products=ProductsOut(precheck=False, secretaria=False),
            plan=catalog.PLAN_FREE,
            secretaria_tier=None,
            status="inactive",
            addons=catalog.default_addons(catalog.PLAN_FREE),
            limits=catalog.compute_limits(catalog.PLAN_FREE),
            usage={},
        )

    # Normalize through the catalog: the full formalized keysets, with whatever the row
    # materialized layered on top — a pre-catalog row (empty `{}` scaffolds) still reads
    # as a coherent, complete shape. Product flags stay the row's own columns (they can
    # legitimately diverge from the plan via an explicit admin override).
    addons = {**catalog.default_addons(ent.plan), **(ent.addons or {})}
    limits = {**catalog.compute_limits(ent.plan, addons), **(ent.limits or {})}
    return EntitlementOut(
        tenant_id=tenant_id,
        clinic_name=clinic_name,
        products=ProductsOut(
            precheck=ent.precheck_enabled,
            secretaria=ent.secretaria_enabled,
        ),
        plan=ent.plan,
        secretaria_tier=catalog.plan_tier(ent.plan),
        status=ent.status,
        addons=addons,
        limits=limits,
        usage=ent.usage or {},
    )


@dataclass(frozen=True)
class Decision:
    """Outcome of a pure, in-process quota check (mirrors the skill).

    Scaffold for a future round (quota/metering is OUT of scope here) — kept off the
    request path. `check_quota` is faithful to the stripe-billing-entitlements skill but
    is NOT wired into `GET /entitlements`.
    """

    allowed: bool
    reason: str | None = None


def check_quota(ent: Entitlement, feature: str, amount: int = 1) -> Decision:
    """Pure, synchronous, in-process entitlement check. No network, no Stripe.

    Unused this round (metering/quota is out of scope); present only to mirror the skill.
    """
    if ent.status not in ("active", "trialing"):
        return Decision(False, "subscription_inactive")
    limit = ent.limits.get(feature)
    if limit is None:
        return Decision(False, f"feature_not_in_plan:{feature}")
    if ent.usage.get(feature, 0) + amount > limit:
        return Decision(False, f"quota_exceeded:{feature}")
    return Decision(True)
