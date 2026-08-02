"""Public launch-waitlist endpoint (CONTRACTS.md §4 + §5): the PRE-LAUNCH buy gate.

ONE unauthenticated route, `POST /public/launch-waitlist`, following the api/demo.py +
api/public_signup.py shape: honeypot first, then an in-process per-IP `SlidingWindowLimiter`
(fail-open, no Redis, CONTRACTS.md §5), then a single isolated write.

WHY it exists: brain-frontend gates every pricing-page buy button behind a code-level
`PRODUCT_LAUNCHED` flag (`app/(site)/_lib/launch.ts`). While that flag is false the click
opens an "Estamos quase lá" modal instead of /cadastro or Stripe Checkout, and the modal
posts here. Flipping the flag to true retires this surface — the endpoint stays harmless.

It creates NO tenant, touches NO entitlement, calls NO Stripe, and enqueues nothing
(CONTRACTS.md §0.4). It is IDEMPOTENT per email (see services.waitlist), so the same
visitor clicking three different plans leaves one row, not three.

The request body (name, email) is NEVER logged — only a stable `id` and whether the row
was new.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.core.ratelimit import client_ip as _client_ip
from brain_api.schemas.waitlist import WaitlistLeadConfirmation, WaitlistLeadCreate
from brain_api.services.waitlist import check_rate_limit, upsert_waitlist_lead

logger = get_logger(__name__)

# main.py does `app.include_router(public_waitlist.router, ...)`, so the module-level name
# MUST be `router` and the path carries the full route (bare APIRouter, no prefix).
router = APIRouter()

# Fixed confirmation copy shown to every accepted lead — identical for a first-time and a
# repeat submission, so the response never reveals whether the email was already on the list.
_CONFIRMATION_MESSAGE = "Prontinho! Avisaremos você por e-mail assim que o lançamento sair."

# Synthetic id returned for a honeypot hit — never a real row (demo.py pattern).
_HONEYPOT_LEAD_ID = "00000000-0000-0000-0000-000000000000"


@router.post(
    "/public/launch-waitlist",
    status_code=status.HTTP_201_CREATED,
    response_model=WaitlistLeadConfirmation,
    summary="Join the launch waiting list",
    description=(
        "Public lead capture for the pre-launch buy gate: while the product cannot be "
        "purchased yet, a click on any pricing CTA lands here instead of checkout. "
        "Idempotent per email; creates no tenant."
    ),
    responses={
        201: {"description": "Lead captured or refreshed (or accept-and-dropped honeypot)."},
        422: {"description": "Validation error (missing name, bad email, oversize field)."},
        429: {"description": "Rate limited (basic per-IP anti-spam)."},
    },
)
async def join_launch_waitlist(
    request: Request,
    payload: WaitlistLeadCreate,
    session: AsyncSession = Depends(get_session),
) -> WaitlistLeadConfirmation:
    """Capture a waitlist lead: honeypot + rate-limit guard, then upsert + confirm."""
    ip = _client_ip(request)

    # HONEYPOT (CONTRACTS.md §5): a filled hidden field means a bot. Silently
    # accept-and-drop — return a normal 201 WITHOUT persisting a row, using a synthetic
    # nil UUID so the response shape stays valid without leaking a real id.
    if payload.website and payload.website.strip():
        logger.info("waitlist_lead_honeypot_dropped")
        return WaitlistLeadConfirmation(id=_HONEYPOT_LEAD_ID, message=_CONFIRMATION_MESSAGE)

    # RATE LIMIT (CONTRACTS.md §5): trip -> 429, fail-open inside check_rate_limit.
    if not check_rate_limit(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Muitas solicitações. Tente novamente em instantes.",
        )

    row = await upsert_waitlist_lead(session, payload)
    # Stable reference only — never log the name or email.
    logger.info("waitlist_lead_captured", id=str(row.id), plan_hint=row.plan_hint)
    return WaitlistLeadConfirmation(id=row.id, message=_CONFIRMATION_MESSAGE)
