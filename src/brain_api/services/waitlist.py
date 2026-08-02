"""Launch-waitlist service: in-process anti-spam + IDEMPOTENT persistence.

Same shape as `services.demo` — one row, no tenant, no entitlement, no Stripe, no async
work (CONTRACTS.md §0.4 / §5) — with one difference: `upsert_waitlist_lead` is
IDEMPOTENT per email instead of append-only.

Idempotency rules (the `waitlist_leads.email` UNIQUE index is the real guard):
- `email` is lowercased before both the lookup and the insert, so case variants of the
  same address collapse to one row.
- On a repeat submission, `name` and `plan_hint` are REFRESHED (the latest click is the
  better sales signal) but `created_at` is left alone — it means "first asked", which is
  what tells us who has been waiting longest.
- A concurrent duplicate races to an IntegrityError; that path re-reads the winning row
  and applies the same refresh, so the caller still gets a 201 and one row exists.

The rate limiter is its own instance ("waitlist"), per core/ratelimit.py's invariant of
one limiter per protected surface: a pre-launch pricing-page form must not be able to eat
the /public/* signup budget that the real funnel will depend on at launch. It is
best-effort and FAIL-OPEN — it must never raise and never 500 lead capture.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.core.ratelimit import SlidingWindowLimiter
from brain_api.models import WaitlistLead
from brain_api.schemas.waitlist import WaitlistLeadCreate

logger = get_logger(__name__)

_limiter = SlidingWindowLimiter("waitlist", lambda: get_settings().WAITLIST_RATE_LIMIT_PER_MIN)


def check_rate_limit(client_ip: str) -> bool:
    """Return True if `client_ip` is under the per-minute limit, else False.

    Allows up to `Settings.WAITLIST_RATE_LIMIT_PER_MIN` requests per 60s sliding window
    per IP (core/ratelimit.py; fail-open — the limiter must never break lead capture).
    """
    return _limiter.allow(client_ip)


def _refresh(row: WaitlistLead, payload: WaitlistLeadCreate) -> WaitlistLead:
    """Apply a repeat submission onto an existing row (never touches `created_at`)."""
    row.name = payload.name
    # Only overwrite with a real value: a later click that carried no plan hint must not
    # erase the plan we already know this lead wanted.
    if payload.plan_hint is not None:
        row.plan_hint = payload.plan_hint
    return row


async def upsert_waitlist_lead(session: AsyncSession, payload: WaitlistLeadCreate) -> WaitlistLead:
    """Persist (or refresh) exactly one `waitlist_leads` row and return it.

    Creates no tenant, touches no entitlement, calls no Stripe. The honeypot `website`
    field is NOT persisted — it never reaches this layer as a column.
    """
    email = payload.email.lower()

    existing = await session.scalar(select(WaitlistLead).where(WaitlistLead.email == email))
    if existing is not None:
        _refresh(existing, payload)
        await session.commit()
        await session.refresh(existing)
        return existing

    row = WaitlistLead(name=payload.name, email=email, plan_hint=payload.plan_hint)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        # Raced: another submission inserted this email between the SELECT above and this
        # commit. The UNIQUE constraint is the real guard — fall back to the winner and
        # apply the same refresh, so the caller still sees a normal success.
        await session.rollback()
        winner = await session.scalar(select(WaitlistLead).where(WaitlistLead.email == email))
        if winner is None:  # pragma: no cover - only reachable if the row vanished mid-race
            raise
        _refresh(winner, payload)
        await session.commit()
        await session.refresh(winner)
        return winner

    await session.refresh(row)
    return row
