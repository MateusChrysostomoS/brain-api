"""PreCheck billing: quota window resolution + usage/spend summary (precheck-billing
round).

PreCheck bills FLAT-PRICE-plus-quota, never metered — the three stripe-billing-
entitlements concerns stay separated the same way as everywhere else in this codebase:
our `usage_events` ledger is the metering source of truth, `entitlements.limits` /
`entitlements.period_*` are the entitlement authority, and Stripe is the payment engine
(it bills a flat plan price plus, optionally, one-off per-unit top-ups — never a meter).

This module answers the two questions both the internal PreCheck-facing quota endpoint
(api/internal_precheck.py) and the tenant-facing usage endpoint (GET /billing/precheck/
usage, upgrade confirmation — api/billing.py) need:

- `quota_window`: which `[start, end)` window does "this period's" usage count against,
  for a given entitlement, right now?
- `usage_summary`: given that window, how many consultations has the tenant used, how
  many are they entitled to (plan quota + unexpired top-up credits), and are they still
  allowed to have another one?

Recording a `precheck_consultations` usage event rides the SAME `services/usage.py::
record_usage` ledger every other feature uses. It deliberately does NOT fire a Stripe
meter event: `_METER_EVENT_SETTINGS` in that module has no entry for
`catalog.LIMIT_PRECHECK_CONSULTATIONS`, so `record_usage`'s meter-forward branch is
skipped entirely for this feature — there is no Stripe Meter for it, because PreCheck is
never billed by usage.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.models import Entitlement, PrecheckTopupCredit, UsageEvent
from brain_api.services import catalog


def _as_utc(dt: datetime) -> datetime:
    """Normalize a DB datetime for comparison (SQLite returns naive; Postgres aware) —
    mirrors `services/signup.py::_as_utc`."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _calendar_month_window(now: datetime) -> tuple[datetime, datetime]:
    """The `[start, end)` of `now`'s UTC calendar month."""
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def quota_window(ent: Entitlement, now: datetime) -> tuple[datetime, datetime]:
    """The `[start, end)` window `precheck_consultations` usage counts against, right now.

    Prefers the tenant's real Stripe billing cycle (`ent.period_start`/`period_end`, kept
    current by the `customer.subscription.*` webhook recompute) whenever BOTH are set AND
    the period has not already ended — an ended-but-not-yet-recomputed period would
    otherwise wrongly window usage into the past. Falls back to the current UTC calendar
    month for a tenant with no Stripe period on record yet (mid-signup, an
    admin-materialized row, or a plan with no Stripe subscription at all) — a predictable,
    always-available window rather than an error.
    """
    if ent.period_start is not None and ent.period_end is not None:
        period_end = _as_utc(ent.period_end)
        if period_end > now:
            return _as_utc(ent.period_start), period_end
    return _calendar_month_window(now)


@dataclass(frozen=True)
class PrecheckUsageSummary:
    """Everything both the internal quota endpoint and the tenant-facing usage route
    need, resolved in one pass."""

    plan: str
    plan_name: str
    precheck_enabled: bool
    enforced: bool
    quota: int
    used: int
    topup_credits: int
    remaining: int
    allowed: bool
    window_start: datetime
    window_end: datetime
    spend_topup_cents: int
    spend_topup_count: int
    spend_currency: str | None


async def usage_summary(
    session: AsyncSession, ent: Entitlement | None, now: datetime
) -> PrecheckUsageSummary:
    """Resolve the full PreCheck usage/quota/spend picture for one entitlement.

    `ent=None` (no entitlement row at all) resolves to a coherent all-zero, not-enforced
    summary rather than raising: callers that need a 404 for "unknown tenant" (the two
    `/internal/precheck/*` routes) check for that BEFORE calling this; the tenant-facing
    `GET /billing/precheck/usage` wants exactly this default instead (200 always, the
    frontend hides the PreCheck section when `precheck_enabled` is false).
    """
    if ent is None:
        start, end = _calendar_month_window(now)
        free_plan = catalog.get_plan(catalog.PLAN_FREE)
        return PrecheckUsageSummary(
            plan=free_plan.id if free_plan is not None else catalog.PLAN_FREE,
            plan_name=free_plan.name if free_plan is not None else catalog.PLAN_FREE,
            precheck_enabled=False,
            enforced=False,
            quota=0,
            used=0,
            topup_credits=0,
            remaining=0,
            allowed=True,
            window_start=start,
            window_end=end,
            spend_topup_cents=0,
            spend_topup_count=0,
            spend_currency=None,
        )

    plan = catalog.get_plan(ent.plan)
    canonical_plan_id = plan.id if plan is not None else ent.plan
    plan_name = plan.name if plan is not None else ent.plan
    precheck_enabled = plan is not None and plan.precheck

    # Effective limits: the SAME plan-base + addon-grants + admin-override merge
    # services.entitlements.resolve_entitlement performs, so an admin's manual `limits`
    # override wins here too.
    addons = {**catalog.default_addons(ent.plan), **(ent.addons or {})}
    limits = {**catalog.compute_limits(ent.plan, addons), **(ent.limits or {})}
    quota = limits.get(catalog.LIMIT_PRECHECK_CONSULTATIONS, 0)
    enforced = precheck_enabled and quota > 0

    start, end = quota_window(ent, now)

    used = (
        await session.scalar(
            select(func.coalesce(func.sum(UsageEvent.amount), 0)).where(
                UsageEvent.tenant_id == ent.tenant_id,
                UsageEvent.feature == catalog.LIMIT_PRECHECK_CONSULTATIONS,
                UsageEvent.created_at >= start,
                UsageEvent.created_at < end,
            )
        )
        or 0
    )

    topup_credits = (
        await session.scalar(
            select(func.coalesce(func.sum(PrecheckTopupCredit.amount), 0)).where(
                PrecheckTopupCredit.tenant_id == ent.tenant_id,
                PrecheckTopupCredit.expires_at > now,
            )
        )
        or 0
    )

    spend_cents, spend_count, spend_currency = (
        await session.execute(
            select(
                func.coalesce(func.sum(PrecheckTopupCredit.amount_total_cents), 0),
                func.count(PrecheckTopupCredit.id),
                func.max(PrecheckTopupCredit.currency),
            ).where(
                PrecheckTopupCredit.tenant_id == ent.tenant_id,
                PrecheckTopupCredit.granted_at >= start,
                PrecheckTopupCredit.granted_at < end,
            )
        )
    ).one()

    remaining = max(0, quota + topup_credits - used)
    allowed = (not enforced) or remaining > 0

    return PrecheckUsageSummary(
        plan=canonical_plan_id,
        plan_name=plan_name,
        precheck_enabled=precheck_enabled,
        enforced=enforced,
        quota=quota,
        used=used,
        topup_credits=topup_credits,
        remaining=remaining,
        allowed=allowed,
        window_start=start,
        window_end=end,
        spend_topup_cents=spend_cents,
        spend_topup_count=spend_count,
        spend_currency=spend_currency,
    )
