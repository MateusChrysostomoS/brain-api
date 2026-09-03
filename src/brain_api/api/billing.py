"""Billing endpoints (stripe-billing-entitlements skill).

Authenticated tenant actions (create Checkout, open the Billing Portal, PreCheck top-up
+ tier upgrade + usage read) and the Stripe webhook. The webhook is the ONLY billing-path
writer of `entitlements.plan/status/addons/limits/period_*` — nothing here trusts the
client to say what was paid; entitlement state always derives from a signature-verified
Stripe event recomputed through the catalog (the PreCheck upgrade route is the one
deliberate exception: it also writes optimistically, see `services.billing.
upgrade_precheck_plan`'s docstring).

The tenant for every route below is resolved SERVER-SIDE from the JWT (`require_tenant`),
never from client input. The webhook verifies `Stripe-Signature` on the RAW body (pure
HMAC via the stripe SDK — no I/O) and dedupes on `event.id` before mutating.
"""

from datetime import UTC, datetime

import stripe
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, require_tenant
from brain_api.config import get_settings
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.models import Entitlement
from brain_api.schemas.billing import (
    CheckoutRequest,
    CheckoutSessionOut,
    PortalSessionOut,
    PrecheckSpendOut,
    PrecheckTopupIn,
    PrecheckUpgradeIn,
    PrecheckUsageOut,
)
from brain_api.services import billing, precheck_billing

logger = get_logger(__name__)

# `main.py` imports `billing.router`; this module-level name MUST be `router`.
router = APIRouter()


def _precheck_usage_out(summary: precheck_billing.PrecheckUsageSummary) -> PrecheckUsageOut:
    """Project a `PrecheckUsageSummary` to the wire shape shared by `GET /billing/
    precheck/usage` and `POST /billing/precheck/upgrade`'s confirmation payload.

    `topup_expires_at` is the current window's end whenever the tenant holds any
    unexpired top-up credit (every credit is granted expiring at ITS window's end — see
    `services.billing._apply_precheck_topup_checkout` — so the current window's end is
    the correct "use them or lose them" date to surface); `None` when there is nothing
    to expire.
    """
    return PrecheckUsageOut(
        plan=summary.plan,
        plan_name=summary.plan_name,
        precheck_enabled=summary.precheck_enabled,
        enforced=summary.enforced,
        quota=summary.quota,
        used=summary.used,
        remaining=summary.remaining,
        topup_credits=summary.topup_credits,
        topup_expires_at=summary.window_end if summary.topup_credits > 0 else None,
        window_start=summary.window_start,
        window_end=summary.window_end,
        spend=PrecheckSpendOut(
            topup_cents=summary.spend_topup_cents,
            topup_count=summary.spend_topup_count,
            currency=summary.spend_currency,
        ),
    )


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
    "/billing/precheck/topup",
    response_model=CheckoutSessionOut,
    summary="Buy avulso PreCheck consultations (per-unit price, client picks the quantity)",
    responses={
        401: {"description": "Missing/invalid token."},
        409: {"description": "Token has no tenant, or the tenant's plan is not PreCheck-enabled."},
        422: {
            "description": (
                "`quantity` outside the allowed purchase bounds "
                "(`quantity_below_minimum` / `quantity_above_maximum`)."
            )
        },
        502: {"description": "Stripe unreachable / API error."},
        503: {"description": "Billing not configured, or the top-up price is not configured."},
    },
)
async def precheck_topup(
    payload: PrecheckTopupIn,
    principal: Principal = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> CheckoutSessionOut:
    """One-off `mode=payment` Checkout Session for `payload.quantity` avulso PreCheck
    consultations, billed at the per-unit top-up price.

    The quantity is the ONE thing the client gets to name here, and it is bounds-checked
    server-side (see `services.billing.create_precheck_topup_checkout_session`) — the
    frontend's own minimum is convenience, not enforcement.
    """
    url = await billing.create_precheck_topup_checkout_session(
        session, principal.tenant_id, payload.quantity
    )
    return CheckoutSessionOut(url=url)


@router.post(
    "/billing/precheck/upgrade",
    response_model=PrecheckUsageOut,
    summary="Swap between the PreCheck tiers (Start/Basic/Advanced)",
    responses={
        401: {"description": "Missing/invalid token."},
        409: {
            "description": (
                "Token has no tenant; tenant's plan is not PreCheck-enabled; already on "
                "the target plan; no active subscription to swap; or the live "
                "subscription carries no item at the current plan's price."
            )
        },
        422: {"description": "Unknown/invalid target PreCheck plan."},
        502: {"description": "Stripe unreachable / API error."},
        503: {"description": "Billing not configured, or a plan price is not configured."},
    },
)
async def precheck_upgrade(
    payload: PrecheckUpgradeIn,
    principal: Principal = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> PrecheckUsageOut:
    """Live Stripe subscription-item price swap + an OPTIMISTIC local entitlement update
    (see `services.billing.upgrade_precheck_plan`); returns the fresh usage summary."""
    summary = await billing.upgrade_precheck_plan(session, principal.tenant_id, payload.plan)
    return _precheck_usage_out(summary)


@router.get(
    "/billing/precheck/usage",
    response_model=PrecheckUsageOut,
    summary="PreCheck consultation usage/quota/spend for the current window",
    responses={
        401: {"description": "Missing/invalid token."},
        409: {"description": "Token has no tenant."},
    },
)
async def precheck_usage(
    principal: Principal = Depends(require_tenant),
    session: AsyncSession = Depends(get_session),
) -> PrecheckUsageOut:
    """Always 200 — zeros/false when the tenant's plan isn't PreCheck-enabled (the
    frontend hides the section then)."""
    ent = await session.get(Entitlement, principal.tenant_id)
    summary = await precheck_billing.usage_summary(session, ent, datetime.now(UTC))
    return _precheck_usage_out(summary)


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
