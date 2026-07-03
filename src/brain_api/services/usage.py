"""Usage-event recording (metering leg — stripe-billing-entitlements skill).

METERING = our local ledger (`usage_events`) + the `entitlements.usage` counters it
feeds. There is NO Stripe call anywhere in this module: meter-event forwarding to
Stripe is a later billing round (see the TODO below). This mirrors the skill's
`record_usage` shape but stays fully synchronous/in-request (no arq worker yet — the
caller side, e.g. secretarIA's reminder job, is expected to fire-and-forget this call).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.core.logging import get_logger
from brain_api.models import Entitlement, UsageEvent
from brain_api.schemas.internal import UsageEventIn

logger = get_logger(__name__)


async def record_usage(session: AsyncSession, payload: UsageEventIn) -> bool:
    """Apply one usage event: idempotent ledger insert + entitlement counter increment.

    Returns `False` (no-op) when `payload.event_id` was already recorded — events get
    redelivered by callers and must not double-count. Otherwise inserts the ledger row
    AND increments `entitlements.usage[feature]` in the SAME transaction (one commit),
    so the marker and the counter can never drift apart; upserts the entitlement row if
    the tenant doesn't have one yet (mirrors `services/admin.py::update_entitlement`).
    """
    if await session.get(UsageEvent, payload.event_id) is not None:
        return False

    session.add(
        UsageEvent(
            id=payload.event_id,
            tenant_id=payload.tenant_id,
            feature=payload.feature,
            amount=payload.amount,
        )
    )

    ent = await session.get(Entitlement, payload.tenant_id)
    if ent is None:
        ent = Entitlement(tenant_id=payload.tenant_id)
        session.add(ent)

    # Whole-dict reassignment triggers JSON change tracking — an in-place `ent.usage[k] +=
    # amount` would not persist (see the entitlement model's docstring).
    current = (ent.usage or {}).get(payload.feature, 0)
    ent.usage = {**(ent.usage or {}), payload.feature: current + payload.amount}

    await session.commit()

    logger.info(
        "usage_recorded",
        tenant_id=str(payload.tenant_id),
        feature=payload.feature,
        amount=payload.amount,
    )

    # TODO(billing round): forward a Stripe meter event for this (feature, amount) once
    # metered add-on prices exist, and check the 80%/100% quota-alert thresholds against
    # `ent.limits[feature]` here (see stripe-billing-entitlements: quota alerts / upsell
    # trigger). Neither happens yet — this round is metering only, no Stripe call.
    return True
