"""Usage-event recording (metering leg — stripe-billing-entitlements skill).

METERING = our local ledger (`usage_events`) + the `entitlements.usage` counters it
feeds. This mirrors the skill's `record_usage` shape but stays fully synchronous/
in-request (no arq worker yet — the caller side, e.g. secretarIA's reminder/onboarding
jobs, is expected to fire-and-forget this call). The Stripe touchpoint is a best-effort
meter event forward for THREE metered features — `billable_patients`,
`active_professionals`, and `reminders` (fully-metered secretaria_basico model,
CONTRACT_onboarding_v1.md §9) — never in the critical path: it fires AFTER the
ledger/counter commit already succeeded and can never raise into the caller (our ledger
is the source of truth; Stripe is a write-only sink). All three features share the exact
same forward/fail-soft mechanics; only the configured Stripe Meter `event_name` differs
(`_METER_EVENT_SETTINGS` below).
"""

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.models import Entitlement, UsageEvent
from brain_api.schemas.internal import UsageEventIn
from brain_api.services import billing, catalog

logger = get_logger(__name__)

#: feature id -> the `Settings` attribute name holding its Stripe Meter `event_name`.
#: All three metered features share the identical fire/never-raise/fail-soft contract
#: (`_forward_meter_event` below) — only the configured event_name differs per feature.
_METER_EVENT_SETTINGS: dict[str, str] = {
    catalog.LIMIT_BILLABLE_PATIENTS: "STRIPE_METER_EVENT_BILLABLE_PATIENTS",
    catalog.LIMIT_ACTIVE_PROFESSIONALS: "STRIPE_METER_EVENT_ACTIVE_PROFESSIONALS",
    catalog.LIMIT_REMINDERS: "STRIPE_METER_EVENT_REMINDERS",
}


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

    meter_setting_name = _METER_EVENT_SETTINGS.get(payload.feature)
    if meter_setting_name is not None:
        event_name = getattr(get_settings(), meter_setting_name)
        await _forward_meter_event(ent, payload.amount, event_name)

    # TODO(billing round): check the 80%/100% quota-alert thresholds against
    # `ent.limits[feature]` here (see stripe-billing-entitlements: quota alerts / upsell
    # trigger). Not implemented yet.
    return True


async def _forward_meter_event(ent: Entitlement, amount: int, event_name: str | None) -> None:
    """Best-effort Stripe meter-event forward for a metered feature (`billable_patients`,
    `active_professionals`, or `reminders` — the three legs of the fully-metered
    secretaria_basico model, CONTRACT_onboarding_v1.md §9). Fires ONLY when `event_name`
    (the feature's configured Stripe Meter `event_name` — `STRIPE_METER_EVENT_BILLABLE_PATIENTS`
    / `STRIPE_METER_EVENT_ACTIVE_PROFESSIONALS` / `STRIPE_METER_EVENT_REMINDERS`, resolved
    by the caller via `_METER_EVENT_SETTINGS`) is configured AND the tenant's entitlement
    has a `stripe_customer_id`. NEVER raises and NEVER blocks the caller's `200` — a
    forwarding failure only means this one usage event doesn't show up on the Stripe
    invoice; our own ledger already won (stripe-billing-entitlements: Stripe meter events
    are write-only and never the source of truth for usage).
    """
    if not event_name or not ent.stripe_customer_id:
        return
    try:
        await billing._stripe_post(
            "/v1/billing/meter_events",
            {
                "event_name": event_name,
                "payload[stripe_customer_id]": ent.stripe_customer_id,
                "payload[value]": str(amount),
            },
        )
    except HTTPException:
        # _stripe_post already logged the specific failure (unconfigured/unreachable/
        # error); this is just the "never let it block the caller" boundary.
        logger.warning("usage_meter_forward_failed", tenant_id=str(ent.tenant_id))
    except Exception:  # noqa: BLE001 - best-effort by contract: never raise, never block.
        logger.warning("usage_meter_forward_error", tenant_id=str(ent.tenant_id), exc_info=True)
