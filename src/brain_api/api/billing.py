"""Billing endpoints (stripe-billing-entitlements skill).

Two authenticated tenant actions (create Checkout, open the Billing Portal) and the
Stripe webhook. The webhook is the ONLY billing-path writer of
`entitlements.plan/status/addons/limits/period_*` — nothing here trusts the client to
say what was paid; entitlement state always derives from a signature-verified Stripe
event recomputed through the catalog.

The tenant for checkout/portal is resolved SERVER-SIDE from the JWT (`require_tenant`),
never from client input. The webhook verifies `Stripe-Signature` on the RAW body (pure
HMAC via the stripe SDK — no I/O) and dedupes on `event.id` before mutating.
"""

import stripe
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, require_tenant
from brain_api.config import get_settings
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.schemas.billing import CheckoutRequest, CheckoutSessionOut, PortalSessionOut
from brain_api.services import billing

logger = get_logger(__name__)

# `main.py` imports `billing.router`; this module-level name MUST be `router`.
router = APIRouter()


@router.post(
    "/billing/checkout",
    response_model=CheckoutSessionOut,
    summary="Start a subscription checkout",
    responses={
        401: {"description": "Missing/invalid token."},
        409: {"description": "Token has no tenant."},
        422: {"description": "Unknown/unassignable plan or unknown add-on."},
        502: {"description": "Stripe unreachable / API error."},
        503: {"description": "Billing not configured (no Stripe key / price)."},
    },
)
async def checkout(
    payload: CheckoutRequest,
    principal: Principal = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> CheckoutSessionOut:
    """Create a Stripe Checkout Session for the authenticated tenant's selection."""
    selection = billing.validate_selection(payload.plan, payload.addons)
    url = await billing.create_checkout_session(session, principal.tenant_id, selection)
    return CheckoutSessionOut(url=url)


@router.post(
    "/billing/portal",
    response_model=PortalSessionOut,
    summary="Open the Stripe Billing Portal",
    responses={
        401: {"description": "Missing/invalid token."},
        409: {"description": "Token has no tenant, or tenant has no billing account yet."},
        502: {"description": "Stripe unreachable / API error."},
        503: {"description": "Billing not configured."},
    },
)
async def portal(
    principal: Principal = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> PortalSessionOut:
    """Let the tenant manage card/plan on Stripe's hosted portal."""
    url = await billing.create_portal_session(session, principal.tenant_id)
    return PortalSessionOut(url=url)


@router.post(
    "/webhooks/stripe",
    summary="Stripe webhook (signature-verified)",
    responses={
        200: {"description": "Received (applied or deduplicated)."},
        400: {"description": "Missing/invalid signature or unconfigured webhook secret."},
    },
)
async def stripe_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    """Verify, dedupe and apply one Stripe event to the local entitlement row.

    Fail CLOSED on auth: an unset STRIPE_WEBHOOK_SECRET rejects every delivery (400)
    rather than accepting unsigned payloads. The raw body is used for the HMAC check
    BEFORE any parsing. Apply errors surface as 500 so Stripe redelivers (the
    idempotency marker commits atomically with the mutation, so a retry reprocesses).
    """
    secret = get_settings().STRIPE_WEBHOOK_SECRET
    sig = request.headers.get("Stripe-Signature")
    payload = await request.body()
    if not secret or not sig:
        logger.warning("stripe_webhook_rejected", reason="secret" if not secret else "signature")
        return JSONResponse({"error": "invalid signature"}, status_code=status.HTTP_400_BAD_REQUEST)
    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except (ValueError, stripe.SignatureVerificationError):
        logger.warning("stripe_webhook_rejected", reason="verification_failed")
        return JSONResponse({"error": "invalid signature"}, status_code=status.HTTP_400_BAD_REQUEST)

    # StripeObject is not dict-iterable — dict(...) would crash; to_dict() is the
    # SDK's plain-dict projection.
    applied = await billing.apply_stripe_event(
        session, event["id"], event["type"], event["data"]["object"].to_dict()
    )
    return JSONResponse({"received": True, "duplicate": not applied})
