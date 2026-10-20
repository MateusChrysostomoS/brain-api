"""Admin control of how many professionals a clinic may keep active (TASK-063).

The cap is `entitlements.limits["professionals"]`, the number the secretarIA reads fresh
(`services/professional_capacity.py` over there) every time a professional is created or
re-activated: past it, the invite answers 409 `professional_limit_reached`; with the
`multi_professional` add-on off, 403 `multi_professional_not_entitled`. So "letting a
clinic have N doctors" is exactly two writes on the entitlement row — the add-on ON and
the limit at N — and that is all this module does. Nothing here deactivates anyone: a
cap below the current roster only blocks the next addition, which is why it is refused.

Who may edit: only rows no Stripe subscription owns (a webhook would silently undo the
edit) and only clinics with the secretarIA on (the cap means nothing elsewhere). Contract
and decisions in `docs/CHECKPOINT_admin_professional_capacity.md`.
"""

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.core.logging import get_logger
from brain_api.models import Entitlement, Tenant
from brain_api.schemas.admin import CapacityBlockedReason, ProfessionalCapacityOut
from brain_api.services import catalog, secretaria_provisioning

logger = get_logger(__name__)


def _blocked_reason(ent: Entitlement | None) -> CapacityBlockedReason | None:
    if ent is not None and ent.stripe_subscription_id:
        return "stripe_managed"
    if ent is None or not ent.secretaria_enabled:
        return "secretaria_disabled"
    return None


def _current_cap(ent: Entitlement | None) -> int:
    if ent is None:
        return 0
    # Same per-key merge `services/entitlements.py::resolve_entitlement` serves the
    # secretarIA, so this screen can never show a number the secretarIA does not use.
    limits = {**catalog.compute_limits(ent.plan, ent.addons or {}), **(ent.limits or {})}
    return int(limits.get(catalog.LIMIT_PROFESSIONALS, 0))


def _addon_on(ent: Entitlement | None) -> bool:
    return bool(ent is not None and (ent.addons or {}).get(catalog.ADDON_MULTI_PROFESSIONAL))


async def _active_count(tenant_id: UUID) -> int | None:
    """Live count from the secretarIA roster; None when it cannot be read (fail-soft)."""
    roster = await secretaria_provisioning.get_professionals(tenant_id)
    if roster is None:
        return None
    return sum(1 for row in roster if row.get("is_active"))


async def _load(session: AsyncSession, tenant_id: UUID) -> Entitlement | None:
    if await session.get(Tenant, tenant_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tenant not found")
    return await session.get(Entitlement, tenant_id)


async def _read_roster_outside_transaction(
    session: AsyncSession, tenant_id: UUID, ent: Entitlement | None
) -> int | None:
    # The roster is a network call; never hold an open DB transaction across it (an
    # "idle in transaction" connection blocks migrations — it has happened here before).
    secretaria_on = ent is not None and ent.secretaria_enabled
    await session.close()
    return await _active_count(tenant_id) if secretaria_on else None


def _out(tenant_id: UUID, ent: Entitlement | None, active: int | None) -> ProfessionalCapacityOut:
    reason = _blocked_reason(ent)
    return ProfessionalCapacityOut(
        tenant_id=tenant_id,
        max_professionals=_current_cap(ent),
        multi_professional=_addon_on(ent),
        active_professionals=active,
        editable=reason is None,
        blocked_reason=reason,
    )


async def get_capacity(session: AsyncSession, tenant_id: UUID) -> ProfessionalCapacityOut:
    """Current cap + live active count. 404 if the tenant does not exist."""
    ent = await _load(session, tenant_id)
    active = await _read_roster_outside_transaction(session, tenant_id, ent)
    return _out(tenant_id, ent, active)


async def set_capacity(
    session: AsyncSession, tenant_id: UUID, max_professionals: int, *, actor_user_id: str
) -> ProfessionalCapacityOut:
    """Set the cap to `max_professionals` and switch the add-on ON. Idempotent.

    409 `stripe_managed` / `secretaria_disabled` when the row is not admin-editable, and
    409 `below_active_professionals` when the cap would sit under the live roster. An
    unreadable roster (None) does not block: lowering never removes anyone, and the
    secretarIA re-checks the cap on every addition.
    """
    ent = await _load(session, tenant_id)
    if (reason := _blocked_reason(ent)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, reason)
    active = await _read_roster_outside_transaction(session, tenant_id, ent)
    if active is not None and max_professionals < active:
        raise HTTPException(status.HTTP_409_CONFLICT, "below_active_professionals")

    # Re-read under a row lock: the roster call ran outside any transaction, so the row
    # may have changed meanwhile (a concurrent admin edit or a Stripe event).
    ent = await session.scalar(
        select(Entitlement).where(Entitlement.tenant_id == tenant_id).with_for_update()
    )
    if (reason := _blocked_reason(ent)) is not None:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, reason)

    previous = _current_cap(ent)
    addons = {
        **catalog.default_addons(ent.plan),
        **(ent.addons or {}),
        catalog.ADDON_MULTI_PROFESSIONAL: True,
    }
    # Whole-dict reassignment is what JSON change tracking sees. Other limit keys keep any
    # manual override they already had; only the professionals cap changes.
    ent.addons = addons
    ent.limits = {
        **catalog.compute_limits(ent.plan, addons),
        **(ent.limits or {}),
        catalog.LIMIT_PROFESSIONALS: max_professionals,
    }
    await session.commit()

    logger.info(
        "admin_professional_capacity_updated",
        actor_user_id=actor_user_id,
        tenant_id=str(tenant_id),
        previous=previous,
        max_professionals=max_professionals,
    )
    return _out(tenant_id, ent, active)
