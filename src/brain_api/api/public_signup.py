"""Public cold-signup endpoints: register-at-first-card -> Stripe Checkout -> poll.

Four UNAUTHENTICATED endpoints, all rate-limited per-IP with ONE shared
`SlidingWindowLimiter` bucket (same in-process/fail-open machinery as demo.py/auth.py —
no Redis, CONTRACTS.md §5):

- `POST  /public/signup-intents`      REGISTER the lead (tenant + owner user + inert
  entitlement + linked intent) with a real password, return a real session (honeypot
  guarded).
- `PATCH /public/signup-intents/{id}` replace the ADD-ON selection on a still-pending
  intent (corrections round, 2026-07-22) — the pre-checkout add-on picker calls this
  right before opening Checkout. The plan itself cannot change here.
- `POST  /public/checkout-sessions`   open a Stripe Checkout Session for that intent.
- `GET   /public/onboarding-status`   poll for the webhook activation + mint the one-time
  onboarding token (a resume-in-another-browser FALLBACK now that the registration session
  already exists), exchanged at `POST /auth/exchange-onboarding-token`.

A FIFTH unauthenticated endpoint, `GET /public/checkout-config`, is deliberately NOT
part of that shared limiter — static, non-secret, no-DB-touch config (trial length +,
since the corrections round, per-add-on Stripe-price availability) that a pricing-page
view must never be able to exhaust the signup budget over (see its own docstring below).

Registration IS the sole tenant/user/inert-entitlement writer (`services.signup.
register_signup`); the Stripe webhook apply path (`services.billing.apply_stripe_event` ->
`services.signup.provision_tenant_from_intent`) is the sole entitlement-ACTIVATION writer.
This split replaces the old "webhook is the sole writer" invariant: the registration
creates the inert tenant+login, the webhook only activates the paid entitlement. The PATCH
route only ever touches `catalog_ids` on a row already created by registration — it is
never a third writer of the tenant/user/entitlement themselves.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.auth import build_session_response
from brain_api.config import get_settings
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.core.ratelimit import SlidingWindowLimiter, client_ip
from brain_api.schemas.auth import TokenResponse
from brain_api.schemas.signup import (
    AddonAvailabilityOut,
    CheckoutConfigOut,
    CheckoutSessionCreate,
    CheckoutSessionOut,
    CourtesyRedeemCreate,
    OnboardingStatusOut,
    SignupIntentCatalogOut,
    SignupIntentCatalogPatchIn,
    SignupIntentCreate,
    SignupRegisterOut,
)
from brain_api.models import Entitlement, Tenant
from brain_api.services import billing, catalog, courtesy as courtesy_service
from brain_api.services import signup as signup_service
from brain_api.services.auth import issue_refresh_token

logger = get_logger(__name__)

# main.py does `app.include_router(public_signup.router, ...)`, so the module-level name
# MUST be `router`; paths carry the full route (bare APIRouter, no prefix).
router = APIRouter()

# ONE shared bucket for all four signup routes (per-IP; in-process, fail-open).
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
    response_model=SignupRegisterOut,
    summary="Register a cold-signup lead (first card)",
    description=(
        "Public lead capture at the FIRST card: creates the tenant + owner user (REAL "
        "password) + inert entitlement + linked intent, and returns a real session. The "
        "owner can log in from any device afterwards, whether or not they finish the "
        "wizard or pay."
    ),
    responses={
        201: {"description": "Registered (or silently accepted for a honeypot hit)."},
        409: {"description": "A user with this email already exists."},
        422: {"description": "Bad catalog selection / weak password / missing field."},
        429: {"description": "Rate limited (per-IP anti-spam)."},
    },
)
async def register_signup(
    request: Request,
    payload: SignupIntentCreate,
    session: AsyncSession = Depends(get_session),
) -> SignupRegisterOut:
    """Register the lead and mint a real session (same shape login produces)."""
    _check_rate_limit(request)

    # HONEYPOT (demo.py pattern): a filled hidden field means a bot. Silently
    # accept-and-drop with a synthetic id + empty session — never persist, never touch the
    # real flow. A real browser leaves `website` empty and never sees this branch.
    if payload.website and payload.website.strip():
        logger.info("signup_intent_honeypot_dropped")
        return SignupRegisterOut(
            intent_id=_HONEYPOT_INTENT_ID, session=TokenResponse(access_token="")
        )

    registration = await signup_service.register_signup(session, payload)
    refresh = await issue_refresh_token(session, registration.user.id)
    logger.info(
        "signup_intent_created",
        intent_id=str(registration.intent.id),
        tenant_id=str(registration.user.tenant_id),
    )
    return SignupRegisterOut(
        intent_id=registration.intent.id,
        session=build_session_response(registration.user, refresh),
    )


@router.patch(
    "/public/signup-intents/{intent_id}",
    response_model=SignupIntentCatalogOut,
    summary="Update the add-on selection on a pending signup intent",
    description=(
        "Replace `catalog_ids` on a still-`pending_payment` intent — the pre-checkout "
        "add-on picker calls this right before `POST /public/checkout-sessions`. "
        "Add-ons are the only mutable part: the plan derived at registration cannot "
        "change here."
    ),
    responses={
        404: {"description": "Unknown signup intent."},
        409: {
            "description": (
                "intent_not_pending | plan_change_not_allowed | addon_not_available"
            )
        },
        422: {"description": "Bad catalog selection (unknown id / not exactly one plan)."},
        429: {"description": "Rate limited (per-IP anti-spam)."},
    },
)
async def update_signup_intent_catalog(
    intent_id: UUID,
    request: Request,
    payload: SignupIntentCatalogPatchIn,
    session: AsyncSession = Depends(get_session),
) -> SignupIntentCatalogOut:
    """Same shared `_limiter` bucket as the other public signup POSTs (`_check_rate_limit`)."""
    _check_rate_limit(request)
    intent = await signup_service.update_intent_catalog(session, intent_id, payload.catalog_ids)
    return SignupIntentCatalogOut(
        intent_id=intent.id, catalog_ids=intent.catalog_ids, status=intent.status
    )


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


@router.post(
    "/public/courtesy-redemptions",
    response_model=OnboardingStatusOut,
    summary="Resgata um cupom de cortesia e ativa a clínica sem pagamento",
    description=(
        "Caminho paralelo ao `/public/checkout-sessions` para acesso de cortesia: "
        "ativa o entitlement do intent na hora, sem cartão e sem assinatura no "
        "Stripe, e devolve a MESMA resposta que o polling de onboarding-status "
        "devolveria — inclusive o token de onboarding, para o navegador seguir "
        "pelo mesmo caminho de entrada."
    ),
    responses={
        200: {"description": "Resgatado: clínica ativa, token de onboarding emitido."},
        404: {"description": "Intent desconhecido."},
        409: {"description": "O intent já saiu de pending_payment (pago ou já resgatado)."},
        422: {"description": "Cupom inválido, expirado, esgotado ou desativado."},
        429: {"description": "Rate limited (per-IP anti-spam)."},
    },
)
async def redeem_courtesy_coupon(
    request: Request,
    payload: CourtesyRedeemCreate,
    session: AsyncSession = Depends(get_session),
) -> OnboardingStatusOut:
    """Resgate do cupom + a ponte do PreCheck, na ordem em que o webhook as faz."""
    _check_rate_limit(request)
    intent = await courtesy_service.redeem(session, payload.intent_id, payload.code)

    # A ponte que provisiona a clínica no PreCheck, igual ao caminho pago
    # (billing.apply_stripe_event): PÓS-commit, best-effort, gated no entitlement
    # que acabou de ser ativado. Sem ela, `precheck_account_links` fica vazio e o
    # handoff `POST /sso/precheck/token` responde 409 — o médico entra em nada.
    if intent.tenant_id is not None:
        ent = await session.get(Entitlement, intent.tenant_id)
        if ent is not None and ent.precheck_enabled:
            tenant = await session.get(Tenant, intent.tenant_id)
            if tenant is not None:
                from brain_api.services import onboarding_sync

                await onboarding_sync.ensure_precheck_provisioned(session, tenant)

    return await signup_service.ready_status(session, intent)


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
    summary="Public checkout-funnel config (trial length + add-on availability)",
    description=(
        "Static, non-secret config the pre-checkout funnel needs: the REAL deployed "
        "trial length (instead of hardcoding a second source of truth), and — since "
        "the 2026-07-22 corrections round — which add-ons have a Stripe price "
        "configured in THIS environment, so the picker only ever offers ones that "
        "would actually check out."
    ),
)
async def checkout_config() -> CheckoutConfigOut:
    """No rate limit — deliberate. Unlike the routes above, this touches no DB and
    returns nothing secret; a pricing-page view must never be able to eat into the
    shared `_limiter` budget that the actual intent/checkout/status flow depends on.
    `addons` reuses `billing.price_id_for` (settings-only lookup, no DB, no Stripe call)
    over `catalog.ADDON_IDS` in stable alphabetical order.
    """
    addons = [
        AddonAvailabilityOut(id=addon_id, available=billing.price_id_for(addon_id) is not None)
        for addon_id in sorted(catalog.ADDON_IDS)
    ]
    return CheckoutConfigOut(
        trial_period_days=get_settings().STRIPE_TRIAL_PERIOD_DAYS, addons=addons
    )
