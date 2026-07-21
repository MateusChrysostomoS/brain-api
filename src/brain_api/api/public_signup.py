"""Public cold-signup endpoints: lead capture -> Stripe Checkout -> onboarding poll.

Three UNAUTHENTICATED endpoints, all rate-limited per-IP with ONE shared
`SlidingWindowLimiter` bucket (same in-process/fail-open machinery as demo.py/auth.py —
no Redis, CONTRACTS.md §5):

- `POST /public/signup-intents`    create the pending-payment intent (honeypot guarded).
- `POST /public/checkout-sessions` open a Stripe Checkout Session for that intent.
- `GET  /public/onboarding-status` poll for provisioning + mint the one-time onboarding
  token the browser exchanges at `POST /auth/exchange-onboarding-token`.

A FOURTH unauthenticated endpoint, `GET /public/checkout-config`, is deliberately NOT
part of that shared limiter — static, non-secret, no-DB-touch config (today just
`STRIPE_TRIAL_PERIOD_DAYS`) that a pricing-page view must never be able to exhaust the
signup budget over (see its own docstring below).

Nothing here ever writes a tenant/user/entitlement — provisioning happens ONLY in the
Stripe webhook apply path (`services.billing.apply_stripe_event` ->
`services.signup.provision_tenant_from_intent`), the same "webhook is the sole writer"
invariant `services/billing.py` documents for the existing tenant billing flow. This
module is pure request validation + intent bookkeeping + Checkout Session creation
(+ that one static config read).
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.core.ratelimit import SlidingWindowLimiter, client_ip
from brain_api.schemas.signup import (
    CheckoutConfigOut,
    CheckoutSessionCreate,
    CheckoutSessionOut,
    OnboardingStatusOut,
    SignupIntentCreate,
    SignupIntentOut,
)
from brain_api.services import signup as signup_service

logger = get_logger(__name__)

# main.py does `app.include_router(public_signup.router, ...)`, so the module-level name
# MUST be `router`; paths carry the full route (bare APIRouter, no prefix).
router = APIRouter()

# ONE shared bucket for all three signup routes (per-IP; in-process, fail-open).
_limiter = SlidingWindowLimiter("signup", lambda: get_settings().SIGNUP_RATE_LIMIT_PER_MIN)

# Synthetic id returned for a honeypot hit — never a real row (demo.py pattern).
_HONEYPOT_INTENT_ID = "00000000-0000-0000-0000-000000000000"


def _check_rate_limit(request: Request) -> None:
    """429 when the client IP exceeds the shared signup budget (fail-open inside)."""
    if not _limiter.allow(client_ip(request)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Muitas solicitações. Tente novamente em instantes.",
        )


@router.post(
    "/public/signup-intents",
    status_code=status.HTTP_201_CREATED,
    response_model=SignupIntentOut,
    summary="Start a cold signup",
    description="Public lead capture + catalog selection. Creates no tenant/user yet.",
    responses={
        201: {"description": "Intent created (or silently accepted for a honeypot hit)."},
        409: {"description": "A user with this email already exists."},
        422: {"description": "Bad catalog selection (unknown id, not exactly one plan, ...)."},
        429: {"description": "Rate limited (per-IP anti-spam)."},
    },
)
async def create_signup_intent(
    request: Request,
    payload: SignupIntentCreate,
    session: AsyncSession = Depends(get_session),
) -> SignupIntentOut:
    """Capture the lead + catalog selection; provisioning happens only after payment."""
    _check_rate_limit(request)

    # HONEYPOT (demo.py pattern): a filled hidden field means a bot. Silently
    # accept-and-drop with a synthetic id — never persist, never touch the real flow.
    if payload.website and payload.website.strip():
        logger.info("signup_intent_honeypot_dropped")
        return SignupIntentOut(intent_id=_HONEYPOT_INTENT_ID)

    intent = await signup_service.create_signup_intent(session, payload)
    logger.info("signup_intent_created", intent_id=str(intent.id))
    return SignupIntentOut(intent_id=intent.id)


@router.post(
    "/public/checkout-sessions",
    response_model=CheckoutSessionOut,
    summary="Open a Stripe Checkout Session for a signup intent",
    responses={
        404: {"description": "Unknown signup intent."},
        409: {"description": "The intent already left pending_payment."},
        429: {"description": "Rate limited (per-IP anti-spam)."},
        502: {"description": "Stripe unreachable / API error."},
        503: {"description": "Billing not configured / price not mapped."},
    },
)
async def create_checkout_session(
    request: Request,
    payload: CheckoutSessionCreate,
    session: AsyncSession = Depends(get_session),
) -> CheckoutSessionOut:
    """Create the Stripe Checkout Session the browser redirects to for payment."""
    _check_rate_limit(request)
    url = await signup_service.create_checkout_session_for_intent(session, payload.intent_id)
    return CheckoutSessionOut(checkout_url=url)


@router.get(
    "/public/onboarding-status",
    response_model=OnboardingStatusOut,
    summary="Poll a signup intent for provisioning status",
    description=(
        'Resolves a Checkout Session id back to its intent. While `status == "ready"` '
        "and unredeemed, mints (and rotates) the one-time onboarding token the browser "
        "exchanges at POST /auth/exchange-onboarding-token."
    ),
    responses={
        404: {"description": "Unknown Stripe Checkout Session id."},
        429: {"description": "Rate limited (per-IP anti-spam)."},
    },
)
async def onboarding_status(
    session_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> OnboardingStatusOut:
    """`pending` while awaiting payment; `ready`/`failed` once the webhook resolved it."""
    _check_rate_limit(request)
    return await signup_service.get_onboarding_status(session, session_id)


@router.get(
    "/public/checkout-config",
    response_model=CheckoutConfigOut,
    summary="Public checkout-funnel config (trial length)",
    description=(
        "Static, non-secret config the pre-checkout funnel needs so its disclosure "
        "copy can quote the REAL deployed trial length instead of hardcoding a second "
        "source of truth."
    ),
)
async def checkout_config() -> CheckoutConfigOut:
    """No rate limit — deliberate. Unlike the three routes above, this touches no DB
    and returns nothing secret; a pricing-page view must never be able to eat into the
    shared `_limiter` budget that the actual intent/checkout/status flow depends on.
    """
    return CheckoutConfigOut(trial_period_days=get_settings().STRIPE_TRIAL_PERIOD_DAYS)
