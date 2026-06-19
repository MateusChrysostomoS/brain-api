"""Entitlement resolution service (stripe-billing-entitlements skill, CONTRACTS.md §3.1).

The runtime authority for "what is this tenant allowed to use, right now". Resolved
synchronously, in-process, from the LOCAL `entitlements` row keyed by tenant_id.

NEVER call Stripe to answer a read: Stripe is a write-only money sink, recomputed INTO
our row by webhooks (a future round). The dashboard reads our counters (`ent.usage`),
not Stripe. This module performs a pure local DB read — no network, no Stripe SDK.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.models import Entitlement, Tenant
from brain_api.schemas.entitlement import EntitlementOut, ProductsOut


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
            plan="free",
            status="inactive",
            addons={},
            limits={},
            usage={},
        )

    return EntitlementOut(
        tenant_id=tenant_id,
        clinic_name=clinic_name,
        products=ProductsOut(
            precheck=ent.precheck_enabled,
            secretaria=ent.secretaria_enabled,
        ),
        plan=ent.plan,
        status=ent.status,
        addons=ent.addons or {},
        limits=ent.limits or {},
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
