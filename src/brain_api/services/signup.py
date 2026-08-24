"""Cold-signup service: register-at-first-card, Stripe Checkout, onboarding poll + activate.

The bridge from a public "stranger fills a form" lead to a paying, logged-in tenant with
NO human in the loop (auth-jwt-multitenant + stripe-billing-entitlements skills).

IMPORTANT — the "who writes what" split (this is the architectural invariant of this
module; it used to say the webhook was the sole tenant/user/entitlement writer):

- `register_signup` is the SOLE writer of the Tenant + owner User + INERT Entitlement.
  It runs the instant the FIRST card is submitted (`POST /public/signup-intents`), with a
  REAL password the visitor chose, and returns a real session. The tenant exists and the
  owner can log in from any device from this point on — whether or not they ever finish
  the wizard or pay. The entitlement it creates is inert (status="inactive", plan="free",
  both products OFF): "registered, nothing purchased yet".
- `provision_tenant_from_intent` is the SOLE writer that ACTIVATES that inert entitlement
  to the purchased plan/add-ons. It runs from the Stripe webhook apply path
  (`services.billing.apply_stripe_event`) on `checkout.session.completed` with
  `metadata.kind == "signup_intent"`. It NO LONGER creates the tenant/user (they already
  exist) — it looks the tenant up via `intent.tenant_id`, flips the entitlement, links the
  Stripe ids, and seeds the onboarding state from `intent.intake`. Idempotent on
  `intent.status == "completed"` (a redelivered event is a no-op). Entitlement state is
  still materialized from the intent's `catalog_ids` (not the `customer.subscription.*`
  event, which can race the webhook) — later `subscription.*` events reconcile via the
  `stripe_customer_id` fallback `_entitlement_for_event` implements.

`register_signup` -> `create_checkout_session_for_intent` -> (Stripe) ->
`provision_tenant_from_intent` (webhook) -> `get_onboarding_status` poll. `attach_intake`
is the authenticated mid-wizard step that stores the eligibility answers on the intent.

The Checkout Session is `mode=setup` (card capture only, no Stripe-authored trial
banner); the Stripe SUBSCRIPTION is created server-side, at webhook time, by
`services.billing._create_subscription_for_signup` — which then passes that subscription's
id AND its full payload (the `status`/period the entitlement is activated with) into
`provision_tenant_from_intent`. This module still writes no Stripe subscription of its
own, and `provision_tenant_from_intent` remains the sole activator, unchanged.

The onboarding token (`get_onboarding_status`) is now a FALLBACK for resuming in a
different browser/tab that never got the registration session: only that synchronous poll
can hand the plaintext back, and only its SHA-256 hash
(`core.security.hash_refresh_token`) is persisted, rotated per poll until exchanged once
(`POST /auth/exchange-onboarding-token`, `api/auth.py`).
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.core.security import hash_password, hash_refresh_token
from brain_api.models import Entitlement, SignupIntent, Tenant, User
from brain_api.models.user import ROLE_DOCTOR
from brain_api.schemas.signup import IntakeIn, OnboardingStatusOut, SignupIntentCreate
from brain_api.services import billing, catalog, onboarding

logger = get_logger(__name__)


def _as_utc(dt: datetime) -> datetime:
    """Normalize a DB datetime for comparison (SQLite returns naive; Postgres aware)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _plan_id_of(catalog_ids: list) -> str | None:
    """The single plan id an intent's `catalog_ids` carries (None if somehow absent —
    schema validation guarantees exactly one at creation time)."""
    return next((cid for cid in catalog_ids if cid in catalog.PLAN_IDS), None)


# --- 1. Registration (the SOLE tenant/user/inert-entitlement writer) -----------------


@dataclass(frozen=True)
class Registration:
    """The result of registering a lead: the intent (drives checkout) + the owner user
    (the caller mints a session from it, same shape login produces)."""

    intent: SignupIntent
    user: User


async def register_signup(session: AsyncSession, payload: SignupIntentCreate) -> Registration:
    """Register a cold-signup lead at the FIRST card, atomically.

    Creates, in one transaction: the Tenant (clinic_name), the owner User (role doctor,
    `is_owner`/`is_manager` both true) with a REAL password hash from the password the
    visitor just chose (NOT a random unknown one — that was the old bug: the owner could
    use the dashboard right after paying but could never log back in), an INERT
    Entitlement (status="inactive", plan="free", both products OFF — "nothing purchased
    yet"), and the SignupIntent already linked to the tenant (`intent.tenant_id`). Mirrors
    `scripts/seed_dev.py`'s bootstrap order (Tenant -> flush -> User -> Entitlement).

    409 `email_already_registered` if a user with this email already exists
    (case-insensitive), checked up front AND enforced again by the DB unique constraint (a
    concurrent duplicate registration races to an IntegrityError -> the same 409). The
    later Stripe webhook only ACTIVATES the inert entitlement — it never creates any of
    these rows.
    """
    email = payload.email.lower()
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "email_already_registered")

    tenant = Tenant(clinic_name=payload.clinic_name)
    session.add(tenant)
    await session.flush()  # assign tenant.id

    user = User(
        email=email,
        name=payload.name,
        password_hash=hash_password(payload.password),
        role=ROLE_DOCTOR,
        is_owner=True,
        is_manager=True,
        tenant_id=tenant.id,
    )
    session.add(user)

    # Inert entitlement: registered, nothing purchased yet. The full formalized keysets of
    # the free plan (every product OFF, every add-on false, every limit 0) — the same
    # `compute_entitlement_state` the webhook activation and admin PATCH both use, so a
    # freshly-registered row reads as a coherent, complete "not entitled" state via
    # `resolve_entitlement` (no 404/500 for the /app NoEntitlementsPanel).
    session.add(
        Entitlement(
            tenant_id=tenant.id,
            status="inactive",
            plan=catalog.PLAN_FREE,
            **catalog.compute_entitlement_state(catalog.PLAN_FREE),
        )
    )

    intent = SignupIntent(
        name=payload.name,
        clinic_name=payload.clinic_name,
        email=email,
        whatsapp_phone=payload.whatsapp_phone,
        catalog_ids=list(payload.catalog_ids),
        intake=payload.intake.model_dump() if payload.intake is not None else None,
        precheck_template_slug=payload.precheck_template_slug,
        tenant_id=tenant.id,
    )
    session.add(intent)
    try:
        await session.commit()
    except IntegrityError:
        # Raced: another registration inserted this email between the check above and this
        # commit. The unique constraint on users.email is the real guard; map it to the
        # same 409 the up-front check returns.
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "email_already_registered") from None
    await session.refresh(intent)
    await session.refresh(user)
    logger.info("signup_registered", intent_id=str(intent.id), tenant_id=str(tenant.id))
    return Registration(intent=intent, user=user)


async def attach_intake(session: AsyncSession, tenant_id: UUID, intake: IntakeIn) -> bool:
    """Store the pre-checkout eligibility answers on the tenant's pending signup intent.

    The authenticated mid-wizard counterpart of registration (`POST /doctor/onboarding/
    intake`): the visitor is logged in by the time the later wizard steps are answered, so
    the intake rides an authenticated call scoped to `principal.tenant_id` rather than a
    second unauthenticated public endpoint. The intake is consumed once, at provisioning,
    by `provision_tenant_from_intent` -> `onboarding.provision_defaults`. Returns False
    (caller answers 204 either way — best-effort, no oracle) when the tenant has no pending
    intent to attach to.
    """
    intent = await session.scalar(
        select(SignupIntent)
        .where(SignupIntent.tenant_id == tenant_id, SignupIntent.status == "pending_payment")
        .order_by(SignupIntent.created_at.desc())
        .limit(1)
    )
    if intent is None:
        return False
    intent.intake = intake.model_dump()
    await session.commit()
    return True


# --- 1b. Catalog update (public, pre-checkout add-on picker) -------------------------


def _normalize_catalog_ids(catalog_ids: list[str]) -> list[str]:
    """Stable, deduped persisted order for a validated catalog selection: the plan id
    first (the schema already guarantees exactly one), then add-on ids in the order
    given, each kept once. A client that resent the plan out of position or repeated an
    add-on id ends up with a clean stored list either way."""
    plan_id = _plan_id_of(catalog_ids)
    ordered: list[str] = [plan_id] if plan_id is not None else []
    for cid in catalog_ids:
        if cid != plan_id and cid not in ordered:
            ordered.append(cid)
    return ordered


async def update_intent_catalog(
    session: AsyncSession, intent_id: UUID, catalog_ids: list[str]
) -> SignupIntent:
    """Replace the catalog selection on a still-pending signup intent (the pre-checkout
    add-on picker, called right before `POST /public/checkout-sessions`).

    Add-ons are the only mutable part — the PLAN itself, once derived at registration,
    cannot change here (swapping plans is a different commercial decision than
    adding/removing an add-on, and would need re-validating eligibility/intake).
    `catalog_ids` has already passed `schemas.signup.SignupIntentCatalogPatchIn`'s
    validation (`_validate_catalog_ids` — same rule as registration: every id known,
    exactly one assignable/non-free plan) by the time this runs; this function checks
    the two rules a schema cannot: state (still pending?) and cross-referencing the
    CURRENT row + the live Stripe price map.

    404 `signup_intent_not_found` for an unknown id. 409 `intent_not_pending` once the
    intent left `pending_payment` (paid or failed — there is nothing left to configure).
    409 `plan_change_not_allowed` when the new selection's derived plan differs from the
    one already stored. 409 `addon_not_available` for any requested add-on with no
    Stripe price configured in `billing.price_id_for` THIS environment — defense in
    depth, since the frontend is only supposed to ever offer add-ons `GET
    /public/checkout-config` reported as `available` (§ the new `addons` field).

    On success, persists `_normalize_catalog_ids(catalog_ids)` (plan first, deduped) and
    returns the updated row.
    """
    intent = await session.get(SignupIntent, intent_id)
    if intent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "signup_intent_not_found")
    if intent.status != "pending_payment":
        raise HTTPException(status.HTTP_409_CONFLICT, "intent_not_pending")

    if _plan_id_of(catalog_ids) != _plan_id_of(intent.catalog_ids):
        raise HTTPException(status.HTTP_409_CONFLICT, "plan_change_not_allowed")

    for addon_id in catalog_ids:
        if addon_id in catalog.ADDON_IDS and billing.price_id_for(addon_id) is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "addon_not_available")

    intent.catalog_ids = _normalize_catalog_ids(catalog_ids)
    await session.commit()
    await session.refresh(intent)
    logger.info("signup_intent_catalog_updated", intent_id=str(intent.id))
    return intent


# --- 2. Checkout Session -------------------------------------------------------------


async def create_checkout_session_for_intent(session: AsyncSession, intent_id: UUID) -> str:
    """Create a SETUP-mode Checkout Session for a pending signup intent (card capture only).

    Deliberately NOT `mode=subscription` anymore: Stripe's hosted page renders its own
    large "X-day free trial" banner for any subscription-mode session carrying a trial,
    and that wording cannot be suppressed while the trial lives inside the Checkout
    Session. A setup-mode session collects and saves a card and nothing else — no
    subscription, no trial, nothing Stripe-authored about billing. The REAL subscription
    (same `trial_period_days` as before) is created server-side by
    `billing._create_subscription_for_signup` when the resulting
    `checkout.session.completed` webhook lands, using the card this session saved.
    Consequently NO `line_items` / `subscription_data` here — Stripe rejects both in setup
    mode; `billing._apply_setup_custom_text` supplies the only billing wording on the page.

    `client_reference_id` / `metadata` carry the INTENT id (never a tenant — none exists
    yet), tagged `metadata[kind]=signup_intent` so `apply_stripe_event` routes the
    resulting `checkout.session.completed` to `_apply_signup_intent_checkout` ->
    `provision_tenant_from_intent` instead of the existing tenant-linking path.

    `billing.validate_selection` is still called up front even though its
    `CheckoutSelection` is no longer used HERE: it is the fail-fast (422/503) on a bad
    catalog/price configuration, and it must fail BEFORE a checkout URL is handed to the
    buyer — not later, at webhook time, with money-adjacent state already in flight.
    """
    intent = await session.get(SignupIntent, intent_id)
    if intent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "signup_intent_not_found")
    if intent.status != "pending_payment":
        raise HTTPException(status.HTTP_409_CONFLICT, "signup_intent_not_pending")

    plan_id = _plan_id_of(intent.catalog_ids)
    addon_ids = [cid for cid in intent.catalog_ids if cid in catalog.ADDON_IDS]
    # Fail-fast only (see the docstring): the selection is re-derived from the SAME
    # catalog_ids at webhook time, where it actually builds the subscription items.
    billing.validate_selection(plan_id, addon_ids)

    settings = get_settings()
    # Signup checkouts get their own return URLs (public onboarding page — the buyer has
    # no session yet), falling back to the shared tenant ones when unset.
    success_url = (
        settings.STRIPE_SIGNUP_CHECKOUT_SUCCESS_URL or settings.STRIPE_CHECKOUT_SUCCESS_URL
    )
    cancel_url = settings.STRIPE_SIGNUP_CHECKOUT_CANCEL_URL or settings.STRIPE_CHECKOUT_CANCEL_URL
    data: dict[str, str] = {
        "mode": "setup",
        # Required in setup mode and ONLY there: with no line items there is no price for
        # Stripe to infer a currency from, and it rejects the request without one.
        "currency": settings.STRIPE_SETUP_CURRENCY,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": str(intent.id),
        "metadata[kind]": "signup_intent",
        "metadata[signup_intent_id]": str(intent.id),
        "customer_email": intent.email,
        # Stripe does NOT create a Customer for a setup-mode session on its own: the
        # default is `customer_creation="if_required"`, and `customer_email` alone does
        # NOT make one required — VERIFIED against the live API, where a completed
        # session came back with `customer: null`. Without a Customer there is nothing to
        # subscribe, so `_apply_signup_intent_checkout` would fall into its no-customer
        # branch on EVERY signup: entitled, provisioned, and never billed. With
        # `always`, the completed session carries a real `customer` id, the SetupIntent's
        # `payment_method` is populated without any `expand`, and that card is already
        # ATTACHED to the customer (also verified live) — which is exactly what
        # `_create_subscription_for_signup` needs.
        "customer_creation": "always",
        # NO `phone_number_collection`: Stripe allows it only in payment/subscription mode
        # and 400s the whole request in setup mode. Nothing is lost — the buyer's number is
        # already captured at registration (`SignupIntent.whatsapp_phone`, first card),
        # which is where every downstream consumer reads it from anyway.
    }
    billing._apply_setup_custom_text(data)

    payload = await billing._stripe_post("/v1/checkout/sessions", data)
    intent.stripe_session_id = payload["id"]
    await session.commit()
    logger.info("signup_checkout_created", intent_id=str(intent.id), plan=plan_id)
    return payload["url"]


# --- Onboarding status + token mint ---------------------------------------------------


async def get_onboarding_status(
    session: AsyncSession, stripe_session_id: str
) -> OnboardingStatusOut:
    """Resolve a Checkout Session id back to its signup intent's provisioning status.

    Mints (and ROTATES on every poll) the one-time onboarding token here — never in the
    webhook — because only this synchronous request/response can hand the PLAINTEXT to
    the browser; only its hash is ever persisted. Once the token has been redeemed
    (`onboarding_token_used_at` set), it stays `null` forever after.
    """
    intent = await session.scalar(
        select(SignupIntent).where(SignupIntent.stripe_session_id == stripe_session_id)
    )
    if intent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "signup_intent_not_found")

    if intent.status == "pending_payment":
        return OnboardingStatusOut(status="pending", products=None, onboarding_token=None)
    if intent.status == "failed":
        return OnboardingStatusOut(status="failed", products=None, onboarding_token=None)

    # completed
    plan = catalog.get_plan(_plan_id_of(intent.catalog_ids))
    products = {
        "secretaria": bool(plan and plan.secretaria),
        "precheck": bool(plan and plan.precheck),
    }

    token: str | None = None
    if intent.onboarding_token_used_at is None:
        token = secrets.token_urlsafe(32)
        intent.onboarding_token_hash = hash_refresh_token(token)
        intent.onboarding_token_expires_at = datetime.now(UTC) + timedelta(
            minutes=get_settings().ONBOARDING_TOKEN_EXPIRE_MINUTES
        )
        await session.commit()

    return OnboardingStatusOut(status="ready", products=products, onboarding_token=token)


# --- 3. Activation (the SOLE entitlement-activation writer; webhook apply path) ------


async def provision_tenant_from_intent(
    session: AsyncSession,
    intent: SignupIntent,
    *,
    stripe_customer_id: str | None,
    stripe_subscription_id: str | None,
    subscription_payload: dict | None = None,
) -> None:
    """ACTIVATE the inert entitlement a paid signup intent implies, on the tenant that
    `register_signup` already created.

    The tenant + owner user + inert entitlement already exist by the time this webhook
    fires (registration built them at the first card). This function therefore:
    - looks the tenant up via `intent.tenant_id` (never creates one);
    - flips its entitlement from inert to the purchased plan/add-ons (materialized from
      `intent.catalog_ids` — the same `compute_entitlement_state` admin PATCH uses);
    - links the Stripe customer/subscription ids;
    - sets `status`/`period_start`/`period_end` from `subscription_payload` when the
      caller has one (see below);
    - seeds the onboarding state machine from `intent.intake`
      (`onboarding.provision_defaults` — the tenant sat in the model-default `pending`
      state until now).

    `subscription_payload` is the `POST /v1/subscriptions` response body of the
    subscription `services.billing._create_subscription_for_signup` just created for this
    signup (setup-mode path). It exists because the entitlement's STATUS is money-critical
    and cannot be hardcoded here: that subscription comes back `trialing`, not `active`,
    and BOTH test-window mechanisms gate on `trialing` — `billing.harden_charge` returns
    early unless `status == "trialing"` (a hardcoded "active" ⇒ a tenant that connects
    WhatsApp is never hardened and rides the whole window free) and the
    `customer.subscription.trial_will_end` handler requires it too (⇒ the auto-cancel is
    never scheduled and a tenant Meta never approved DOES get charged, contradicting the
    checkout page's own promise). The `customer.subscription.created` event Stripe fires
    for that POST would normally set the status, but it can arrive while this handler is
    still mid-transaction and be dropped as an unresolved tenant (`_entitlement_for_event`
    falls back to a `stripe_customer_id` lookup against the very column this call is in
    the middle of writing). `None` (the legacy subscription-mode path and the no-Stripe-ids
    case) keeps the historical hardcoded `"active"`.

    Idempotent on `intent.status == "completed"` (a redelivered `checkout.session.
    completed` is a no-op). NO LONGER keyed on `intent.tenant_id` — that is set at
    registration now, so it is always non-null here. Does NOT call the secretaria
    provisioning bridge (`secretaria_provisioned_at` stays null here) — that I/O is fired
    post-commit by `services.billing.apply_stripe_event`.
    """
    if intent.status == "completed":
        return

    tenant = await session.get(Tenant, intent.tenant_id) if intent.tenant_id else None
    if tenant is None:
        # Defensive: registration always links a tenant, so this only happens for an
        # intent that predates the register-at-first-card split (or a deleted tenant).
        # Fail the intent WITHOUT raising — the webhook must still ack and mark the Stripe
        # event processed (billing.apply_stripe_event commits the ProcessedStripeEvent row
        # around this call regardless of the outcome).
        intent.status = "failed"
        intent.failure_reason = "tenant_missing"
        logger.warning("signup_activation_tenant_missing", intent_id=str(intent.id))
        return

    now = datetime.now(UTC)

    # Task 2 (test-window reframe): payment completion starts the Meta/WABA acceptance
    # test window. Only when unset — a redelivered event reaching this point (the
    # `intent.status == "completed"` guard above already short-circuits the common replay
    # case, but this stays defensive) must never push the deadline back out.
    if tenant.test_window_started_at is None:
        tenant.test_window_started_at = now

    # Seed the onboarding state from the (by-now-attached) intake, right before the
    # secretaria bridge fires. provision_defaults overwrites the model-default `pending`
    # baseline registration left; the tenant hasn't connected/paid before this, so this is
    # the first and only meaningful seed.
    onboarding.provision_defaults(tenant, intent.intake, now)

    plan_id = _plan_id_of(intent.catalog_ids)
    addon_ids = [cid for cid in intent.catalog_ids if cid in catalog.ADDON_IDS]
    state = catalog.compute_entitlement_state(plan_id, {aid: True for aid in addon_ids})

    ent = await session.get(Entitlement, tenant.id)
    if ent is None:
        # Defensive: registration creates the inert row, but tolerate its absence (e.g. a
        # pre-split tenant) rather than crash the webhook.
        ent = Entitlement(tenant_id=tenant.id)
        session.add(ent)
    # Status comes from the subscription we just created when we have it (it reports
    # `trialing`, and the test-window machinery gates on exactly that — see the
    # docstring); unknown Stripe statuses fail CLOSED to "inactive", the same mapping the
    # `customer.subscription.*` handler applies. No payload ⇒ the legacy "active".
    if subscription_payload and subscription_payload.get("status"):
        ent.status = billing._STATUS_MAP.get(str(subscription_payload["status"]), "inactive")
        ent.period_start = billing._period_dt(subscription_payload.get("current_period_start"))
        ent.period_end = billing._period_dt(subscription_payload.get("current_period_end"))
    else:
        ent.status = "active"
    ent.plan = plan_id
    ent.stripe_customer_id = stripe_customer_id
    ent.stripe_subscription_id = stripe_subscription_id
    ent.precheck_enabled = state["precheck_enabled"]
    ent.secretaria_enabled = state["secretaria_enabled"]
    # Whole-dict reassignment triggers JSON change tracking (no flag_modified needed).
    ent.addons = state["addons"]
    ent.limits = state["limits"]

    intent.stripe_customer_id = stripe_customer_id
    intent.stripe_subscription_id = stripe_subscription_id
    intent.status = "completed"
    logger.info("signup_activated", intent_id=str(intent.id), tenant_id=str(tenant.id))


# --- Onboarding-token exchange (redeemed by api/auth.py) ------------------------------


@dataclass(frozen=True)
class OnboardingExchange:
    """A successful onboarding-token redemption: the resolved owner user + the intent id
    (for the caller's audit log — the token itself is never logged)."""

    user: User
    intent_id: UUID


async def exchange_onboarding_token(
    session: AsyncSession, raw_token: str
) -> OnboardingExchange | None:
    """Redeem a onboarding token minted by `GET /public/onboarding-status`.

    Returns None (caller answers 401 `invalid_onboarding_token`) when the token is
    unknown, expired, already used, or its intent never finished provisioning. On
    success: burns the token (sets `used_at`, nulls the hash) and returns the tenant's
    owner user, in the SAME commit.
    """
    intent = await session.scalar(
        select(SignupIntent).where(
            SignupIntent.onboarding_token_hash == hash_refresh_token(raw_token)
        )
    )
    if (
        intent is None
        or intent.onboarding_token_used_at is not None
        or intent.onboarding_token_expires_at is None
        or _as_utc(intent.onboarding_token_expires_at) <= datetime.now(UTC)
        or intent.tenant_id is None
    ):
        return None

    user = await session.scalar(
        select(User).where(User.tenant_id == intent.tenant_id, User.is_owner.is_(True))
    )
    if user is None:
        return None

    intent.onboarding_token_used_at = datetime.now(UTC)
    intent.onboarding_token_hash = None
    await session.commit()
    return OnboardingExchange(user=user, intent_id=intent.id)
