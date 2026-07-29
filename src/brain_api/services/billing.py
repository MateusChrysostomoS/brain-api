"""Stripe billing service (stripe-billing-entitlements skill).

The three concerns stay separated:
- ENTITLEMENT: the local `entitlements` row — recomputed HERE (webhook apply path) via
  the same `catalog.compute_entitlement_state` the admin PATCH uses. Reads never call
  Stripe.
- BILLING: Stripe owns money. This module talks to it in exactly two places, both
  explicit user actions (create a Checkout Session, open the Billing Portal) — async
  httpx, form-encoded, never in a read path.
- METERING: out of scope this round (`usage` stays a scaffold).

Price ids live in `Settings.STRIPE_PRICE_MAP` (per-environment JSON: catalog id ->
Stripe price id) — the catalog declares WHAT is sellable, the environment declares the
Stripe price that charges for it. Nothing is hardcoded here.

The webhook apply functions are ALMOST pure local-DB writes: the event payload
(subscription items, status, period) is the input, and there is NO Stripe API call while
applying — with exactly TWO deliberate exceptions:
- `customer.subscription.trial_will_end` does a LIVE `GET /v1/subscriptions/{id}` verify
  (plus the `cancel_at` POST it may schedule) because the local row can lag Stripe (§13.6);
- `checkout.session.completed` for a COLD SIGNUP now CREATES the subscription itself
  (`_apply_signup_intent_checkout` -> `_create_subscription_for_signup`: a
  `GET /v1/setup_intents/{id}` + a `POST /v1/subscriptions`), because the signup Checkout
  Session is `mode=setup` (card capture only, no Stripe-authored trial banner) and the
  real subscription is therefore ours to create server-side. It is guarded against ever
  minting a SECOND subscription for one signup intent by, in order: the
  `processed_stripe_events` dedup row, an `intent.status == "completed"` check made BEFORE
  any Stripe call (an already-activated intent reuses `intent.stripe_subscription_id`),
  and only then a DETERMINISTIC `Idempotency-Key` (`signup-sub-{intent_id}`) covering the
  residual window where our own crash between the Stripe call and the local commit rolls
  the dedup row back with everything else. The key is LAST on that list on purpose:
  Stripe expires idempotency keys after ~24h but redelivers a failing webhook for ~3 days.
Both exceptions run their network calls BEFORE any local write, so a Stripe failure
raises out of the handler with nothing half-committed and Stripe simply redelivers.
The webhook route verifies the signature (pure HMAC) and dedupes on `event.id`
(`processed_stripe_events`, same transaction as the mutation). brain-api deliberately
has no Redis/arq (CONTRACTS.md §5), so the apply runs in-request — acceptable because
it is a handful of local writes plus (signup only) two Stripe calls, not heavy work; the
skill's "enqueue" guidance targets recomputes that call out.

Cold-signup checkouts (services/signup.py) ride the SAME `checkout.session.completed`
event and carry the SIGNUP INTENT id (never a tenant id) in `metadata`/
`client_reference_id`: `apply_stripe_event` recognizes them by
`metadata.kind == "signup_intent"` and routes to `services.signup.
provision_tenant_from_intent` INSTEAD of the tenant-linking logic below (which would
otherwise misread the intent id riding `client_reference_id` as a tenant id). That path
ACTIVATES the inert entitlement `services.signup.register_signup` already created at the
buyer's first card — it no longer creates the tenant/user. Every other event — including
an "existing_tenant" checkout — is completely unaffected.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.models import Entitlement, ProcessedStripeEvent, SignupIntent, Tenant
from brain_api.services import catalog

logger = get_logger(__name__)

# Stripe subscription statuses -> our entitlement statuses. Anything unknown or
# not-yet-paid (incomplete / incomplete_expired / paused / unpaid) maps to "inactive"
# so gates fail closed.
_STATUS_MAP = {
    "active": "active",
    "trialing": "trialing",
    "past_due": "past_due",
    "canceled": "canceled",
}


# --- Price map (catalog id <-> Stripe price id) -----------------------------


#: Suffixes marking a plan's THREE metered companion prices (CONTRACT_onboarding_v1.md
#: §9; fully-metered secretaria_basico model — NO flat/anchor price on the plan at all):
#: additional Checkout line items for the same plan, billed by usage rather than a flat
#: fee, one per Stripe Meter (patients, professionals, reminders sent outside the 24h
#: window). Not catalog ids of their own — `{plan_id}_metered_patients` /
#: `{plan_id}_metered_professionals` / `{plan_id}_metered_reminders` are synthetic
#: STRIPE_PRICE_MAP keys recognized only by `_parse_price_map`'s validation,
#: `price_id_for`'s callers, and `_state_from_subscription`'s reverse-lookup (plan-
#: resolution evidence when a subscription carries no anchor-price item at all).
METERED_PATIENTS_SUFFIX = "_metered_patients"
METERED_PROFESSIONALS_SUFFIX = "_metered_professionals"
METERED_REMINDERS_SUFFIX = "_metered_reminders"

#: SUPERSEDED single-companion convention (pre-fully-metered model, one companion price
#: per plan). Still ACCEPTED by `_parse_price_map` — recognized-but-unused — so a
#: deployed STRIPE_PRICE_MAP not yet migrated off it cannot blow up every billing call
#: (the parser raises loudly on unknown ids); no code path reads it anymore.
METERED_SUFFIX = "_metered"


@lru_cache(maxsize=8)
def _parse_price_map(raw: str) -> dict[str, str]:
    """Parse STRIPE_PRICE_MAP (keyed by the raw string so a settings monkeypatch in
    tests gets its own cache slot). Keys are normalized through the catalog's
    LEGACY_PLAN_ALIASES (the deployed map may still say "secretaria_ferro" for what the
    catalog now calls secretaria_basico), then unknown catalog ids are rejected loudly at
    parse time — a typo'd map must not silently unsell a product. `{plan_id}_metered_patients`
    / `{plan_id}_metered_professionals` / `{plan_id}_metered_reminders` keys (e.g.
    "secretaria_basico_metered_patients") are ALSO accepted: the three metered companion
    prices for a plan's Checkout line items, not catalog ids of their own (§9). The
    SUPERSEDED single-companion `{plan_id}_metered` key is ALSO accepted, purely for
    graceful degradation (recognized-but-unused — see METERED_SUFFIX) so a deployed map
    not yet cleaned up doesn't break every billing call.
    """
    try:
        mapping = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"STRIPE_PRICE_MAP is not valid JSON: {exc}") from exc
    normalized = {
        catalog.LEGACY_PLAN_ALIASES.get(str(k), str(k)): str(v) for k, v in mapping.items()
    }
    known = catalog.PLAN_IDS | catalog.ADDON_IDS
    metered_known = {
        f"{plan_id}{suffix}"
        for plan_id in catalog.PLAN_IDS
        for suffix in (
            METERED_PATIENTS_SUFFIX,
            METERED_PROFESSIONALS_SUFFIX,
            METERED_REMINDERS_SUFFIX,
            METERED_SUFFIX,
        )
    }
    unknown = set(normalized) - known - metered_known
    if unknown:
        raise ValueError(f"STRIPE_PRICE_MAP has unknown catalog ids: {sorted(unknown)}")
    return normalized


def price_id_for(catalog_id: str) -> str | None:
    """The Stripe price id selling a catalog plan/add-on (or a `{plan_id}_metered_patients`
    / `{plan_id}_metered_professionals` companion price) in THIS environment."""
    return _parse_price_map(get_settings().STRIPE_PRICE_MAP).get(catalog_id)


def catalog_id_for_price(price_id: str) -> str | None:
    """Reverse lookup: which catalog id a Stripe price id sells (webhook recompute)."""
    for cid, pid in _parse_price_map(get_settings().STRIPE_PRICE_MAP).items():
        if pid == price_id:
            return cid
    return None


# --- Stripe API calls (checkout + portal only; NEVER in a read path) --------


def _stripe_error_fields(resp: httpx.Response) -> dict[str, str | None]:
    """Stripe's own `error.code` / `error.message` off a failed response, for the WARNING
    log in `_stripe_post`/`_stripe_get`.

    Neither field carries a secret (they name the offending PARAMETER and why Stripe
    refused it — e.g. `parameter_unknown` / "Received unknown parameter:
    phone_number_collection"), and they are precisely what turns a blind
    `stripe_api_error upstream_status=400` into an actionable line: the two
    already-fixed setup-mode bugs (missing `currency`, rejected
    `phone_number_collection`) were only found by probing the live API by hand because
    these fields were being thrown away. Parsed DEFENSIVELY — an error body is not
    guaranteed to be JSON (a gateway/HTML 502 is not), and logging must never be the
    thing that raises. The request payload is deliberately NOT logged.
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - any parse failure just means "no fields to log"
        return {"error_code": None, "error_message": None}
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return {"error_code": None, "error_message": None}
    code = error.get("code") or error.get("type")
    message = error.get("message")
    return {
        "error_code": str(code) if code is not None else None,
        "error_message": str(message) if message is not None else None,
    }


async def _stripe_post(
    path: str, data: dict[str, str], *, idempotency_key: str | None = None
) -> dict[str, Any]:
    """Form-encoded POST to the Stripe API. 503 when unconfigured, 502 on failure.

    Stripe's error `code`/`message` are logged at WARNING (`_stripe_error_fields`) without
    the request payload; the secret key is only ever in the auth tuple, never logged.

    `idempotency_key` (optional, off by default for every existing caller) is sent as
    Stripe's `Idempotency-Key` header: replaying a POST with the SAME key returns the
    ORIGINAL object instead of creating a second one. Used by the cold-signup
    subscription create (`_create_subscription_for_signup`), whose webhook handler can
    legitimately run more than once for one signup intent.
    """
    settings = get_settings()
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "billing_not_configured")
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    try:
        async with httpx.AsyncClient(
            base_url=settings.STRIPE_API_BASE, timeout=settings.STRIPE_TIMEOUT_SECONDS
        ) as client:
            resp = await client.post(
                path, data=data, auth=(settings.STRIPE_SECRET_KEY, ""), headers=headers
            )
    except httpx.RequestError as exc:
        logger.warning("stripe_unreachable", path=path)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "stripe_unavailable") from exc
    if resp.status_code >= 400:
        logger.warning(
            "stripe_api_error",
            path=path,
            upstream_status=resp.status_code,
            **_stripe_error_fields(resp),
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "stripe_error")
    return resp.json()


async def _stripe_get(path: str) -> dict[str, Any]:
    """GET counterpart of `_stripe_post` (same 503/502 error mapping). Reads of Stripe's
    own state, deliberately rare — every other Stripe touchpoint in this module is a POST
    (checkout, portal, cancel scheduling, charge hardening). Two callers:
    - the `customer.subscription.trial_will_end` handler's live-subscription verification
      (§13.6), justified because the local `entitlements` row can lag behind what Stripe
      actually did (see the handler's own comments);
    - `_create_subscription_for_signup`, which reads the completed SetupIntent to learn
      which saved card to bill (the cold-signup Checkout Session is `mode=setup`, so the
      card id exists only on Stripe's side).
    `api/onboarding.py`'s test-window restart also calls it directly (payment methods +
    live subscription).
    """
    settings = get_settings()
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "billing_not_configured")
    try:
        async with httpx.AsyncClient(
            base_url=settings.STRIPE_API_BASE, timeout=settings.STRIPE_TIMEOUT_SECONDS
        ) as client:
            resp = await client.get(path, auth=(settings.STRIPE_SECRET_KEY, ""))
    except httpx.RequestError as exc:
        logger.warning("stripe_unreachable", path=path)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "stripe_unavailable") from exc
    if resp.status_code >= 400:
        logger.warning(
            "stripe_api_error",
            path=path,
            upstream_status=resp.status_code,
            **_stripe_error_fields(resp),
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "stripe_error")
    return resp.json()


@dataclass(frozen=True)
class CheckoutSelection:
    """A validated purchase: one assignable plan + optional extra add-ons."""

    plan_id: str
    addon_ids: tuple[str, ...]


def validate_selection(plan_id: str, addon_ids: list[str] | None) -> CheckoutSelection:
    """Resolve + validate a purchase request against the catalog and the price map.

    422 for an unknown/unassignable plan or unknown add-on; 503 when a selected item
    has no Stripe price in this environment (sellable in the catalog but not wired).
    Add-ons the plan already implies are dropped (the combo already charges for them).

    A FULLY METERED plan (CONTRACT_onboarding_v1.md §9 — e.g. secretaria_basico: no flat/
    anchor price, only the three `_metered_patients`/`_metered_professionals`/
    `_metered_reminders` companion prices) WAIVES the plan's own `price_id_for(plan.id)`
    requirement, but ONLY when ALL THREE companions are configured — a partially
    configured set (e.g. professionals + patients but not reminders) would check out a
    subscription missing one metered price entirely, so that dimension of usage would
    accrue in our ledger but never actually get invoiced (silent under-billing).
    `price_not_configured:{plan.id}` is raised whenever the plan has NEITHER a direct
    price NOR all three companions. Add-ons are unaffected by this waiver — each still
    requires its own price regardless of how the plan itself is billed.
    """
    plan = catalog.get_plan(plan_id)
    if plan is None or plan.id not in catalog.ASSIGNABLE_PLAN_IDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown_or_unassignable_plan:{plan_id}"
        )
    addons: list[str] = []
    for addon_id in addon_ids or []:
        if addon_id not in catalog.ADDON_IDS:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown_addon:{addon_id}")
        if addon_id not in plan.included_addons and addon_id not in addons:
            addons.append(addon_id)

    plan_has_direct_price = price_id_for(plan.id) is not None
    plan_is_fully_metered = all(
        price_id_for(f"{plan.id}{suffix}") is not None
        for suffix in (
            METERED_PATIENTS_SUFFIX,
            METERED_PROFESSIONALS_SUFFIX,
            METERED_REMINDERS_SUFFIX,
        )
    )
    if not plan_has_direct_price and not plan_is_fully_metered:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"price_not_configured:{plan.id}")
    for addon_id in addons:
        if price_id_for(addon_id) is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"price_not_configured:{addon_id}"
            )
    return CheckoutSelection(plan_id=plan.id, addon_ids=tuple(addons))


def _selection_price_items(selection: CheckoutSelection) -> list[tuple[str, str | None]]:
    """The ordered `(price_id, quantity)` list a validated selection implies — the ONE
    price-list builder shared by Checkout Session line items (`_append_checkout_line_items`)
    AND subscription-create items (`_append_subscription_items`, Task 2's test-window
    restart): same price/quantity shape, just a different Stripe form-key prefix
    (`line_items[i]` vs `items[i]`). `quantity` is `None` for a metered price — Stripe
    rejects a quantity field on those.

    Fully-metered billing (CONTRACT_onboarding_v1.md §9, secretaria_basico model — NO
    flat/anchor price on the plan): the plan itself only gets an item when it HAS a direct
    price (`validate_selection` may have waived that requirement); either way, EACH
    configured `{plan_id}_metered_patients` / `{plan_id}_metered_professionals` /
    `{plan_id}_metered_reminders` companion is appended as an ADDITIONAL item with no
    quantity. The three companions are independent: any subset (including none, for a
    purely flat-fee plan) may be configured.
    """
    items: list[tuple[str, str | None]] = []
    plan_price_id = price_id_for(selection.plan_id)
    if plan_price_id:
        items.append((plan_price_id, "1"))
    for addon_id in selection.addon_ids:
        items.append((price_id_for(addon_id) or "", "1"))
    for suffix in (METERED_PATIENTS_SUFFIX, METERED_PROFESSIONALS_SUFFIX, METERED_REMINDERS_SUFFIX):
        companion_price_id = price_id_for(f"{selection.plan_id}{suffix}")
        if companion_price_id:
            items.append((companion_price_id, None))
    return items


def _append_checkout_line_items(data: dict[str, str], selection: CheckoutSelection) -> None:
    """Populate `line_items[i][price]`/`[quantity]` for a validated selection, shared by
    BOTH checkout builders (this module's `create_checkout_session` and
    `services.signup.create_checkout_session_for_intent`). See `_selection_price_items`
    for the shape/ordering this projects into Checkout's form-key convention.
    """
    for index, (price_id, quantity) in enumerate(_selection_price_items(selection)):
        data[f"line_items[{index}][price]"] = price_id
        if quantity is not None:
            data[f"line_items[{index}][quantity]"] = quantity


def _append_subscription_items(data: dict[str, str], selection: CheckoutSelection) -> None:
    """Populate `items[i][price]`/`[quantity]` for a validated selection — the subscription-
    create counterpart of `_append_checkout_line_items` (Stripe's `POST /v1/subscriptions`
    uses `items[i]`, not `line_items[i]`). Used by Task 2's
    `POST /doctor/onboarding/test-window/restart` when the tenant's old subscription is
    already canceled and a brand-new one has to be created directly (no Checkout Session,
    since the tenant already has a saved card). Same price/quantity shape as
    `_selection_price_items` — see its docstring.
    """
    for index, (price_id, quantity) in enumerate(_selection_price_items(selection)):
        data[f"items[{index}][price]"] = price_id
        if quantity is not None:
            data[f"items[{index}][quantity]"] = quantity


def _apply_trial(data: dict[str, str]) -> None:
    """Add `subscription_data[trial_period_days]` when configured (> 0); shared by both
    checkout builders (CONTRACT_onboarding_v1.md §9).

    Also sets `custom_text[submit][message]` — Stripe's OWN hosted Checkout page renders
    a prominent default banner for any trial ("X-day free trial"), which reads as a
    courtesy discount and is exactly the framing Task 2's corrections round removed from
    every screen THIS codebase controls. Stripe's default banner wording can't be turned
    off without removing the trial mechanism itself, so this custom text is the one lever
    available on the page where the charge actually happens: it states outright that
    nothing is charged yet, names the real reason for the delay (WhatsApp Coexistence
    connection approval, not a free-trial courtesy), and gives the hard day cap."""
    days = get_settings().STRIPE_TRIAL_PERIOD_DAYS
    if days <= 0:
        return
    data["subscription_data[trial_period_days]"] = str(days)
    data["custom_text[submit][message]"] = (
        f"Você ainda não será cobrado. Este é um período de teste de conexão do seu "
        f"número com a API do WhatsApp Coexistence — no máximo {days} dias até a primeira "
        f"cobrança, que só acontece se a Meta aprovar a conexão dentro desse prazo. Sem "
        f"aprovação a tempo, a assinatura é cancelada automaticamente e você não paga nada."
    )


def _apply_setup_custom_text(data: dict[str, str]) -> None:
    """`custom_text[submit][message]` for a SETUP-mode Checkout Session (the cold-signup
    flow, `services.signup.create_checkout_session_for_intent`) — same settings read and
    same `> 0` day gate as `_apply_trial`, but standalone wording.

    Why a separate helper: a setup-mode session carries no subscription and no trial at
    all, so Stripe renders NOTHING about billing on that page — not even the "X-day free
    trial" banner `_apply_trial`'s text exists to reframe. The trial now lives on the
    subscription OUR webhook creates afterwards (`_create_subscription_for_signup`), which
    the buyer never sees. That makes this the ONLY place the real deal can be stated, and
    it has to start from "you're saving your card" rather than "you won't be charged yet":
    setup mode literally never charges anything, so the honest framing is what the saved
    card is FOR (the Meta/WhatsApp Coexistence connection-test window) and when it can
    first be billed.
    """
    days = get_settings().STRIPE_TRIAL_PERIOD_DAYS
    if days <= 0:
        return
    data["custom_text[submit][message]"] = (
        f"Aqui você apenas salva seu cartão — nada é cobrado agora. Este é o início do "
        f"período de teste de conexão do seu número com a API do WhatsApp Coexistence: no "
        f"máximo {days} dias até a primeira cobrança, que só acontece se a Meta aprovar a "
        f"conexão dentro desse prazo. Sem aprovação a tempo, a assinatura é cancelada "
        f"automaticamente e você não paga nada."
    )


async def create_checkout_session(
    session: AsyncSession, tenant_id: UUID, selection: CheckoutSelection
) -> str:
    """Create a subscription-mode Checkout Session for the tenant; return its URL.

    `tenant_id` rides in `metadata` AND `subscription_data.metadata` so every later
    subscription webhook can resolve the tenant without trusting anything client-side.
    An existing `stripe_customer_id` is reused so upgrades attach to the same customer.
    """
    settings = get_settings()
    ent = await session.get(Entitlement, tenant_id)

    data: dict[str, str] = {
        "mode": "subscription",
        "success_url": settings.STRIPE_CHECKOUT_SUCCESS_URL,
        "cancel_url": settings.STRIPE_CHECKOUT_CANCEL_URL,
        "client_reference_id": str(tenant_id),
        "metadata[kind]": "existing_tenant",
        "metadata[tenant_id]": str(tenant_id),
        "subscription_data[metadata][tenant_id]": str(tenant_id),
    }
    _append_checkout_line_items(data, selection)
    _apply_trial(data)
    if ent is not None and ent.stripe_customer_id:
        data["customer"] = ent.stripe_customer_id

    payload = await _stripe_post("/v1/checkout/sessions", data)
    logger.info(
        "billing_checkout_created",
        tenant_id=str(tenant_id),
        plan=selection.plan_id,
        addons=list(selection.addon_ids),
    )
    return payload["url"]


async def create_portal_session(session: AsyncSession, tenant_id: UUID) -> str:
    """Open the Stripe Billing Portal for the tenant; return its URL.

    409 when the tenant has no Stripe customer yet (nothing to manage — the portal
    manages an existing subscription/card; checkout is the entry point).
    """
    ent = await session.get(Entitlement, tenant_id)
    if ent is None or not ent.stripe_customer_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "no_billing_account")
    payload = await _stripe_post(
        "/v1/billing_portal/sessions",
        {
            "customer": ent.stripe_customer_id,
            "return_url": get_settings().STRIPE_PORTAL_RETURN_URL,
        },
    )
    logger.info("billing_portal_opened", tenant_id=str(tenant_id))
    return payload["url"]


# --- Webhook apply path (pure local DB; the ONLY billing writer of entitlements) ----


async def _entitlement_for_event(session: AsyncSession, obj: dict[str, Any]) -> Entitlement | None:
    """Resolve the entitlement row an event object refers to (fail: None, logged).

    Resolution order: our own `metadata.tenant_id` (stamped at checkout — the trusted
    link), then `client_reference_id`, then a lookup by `customer` id for events that
    carry no metadata (e.g. invoices). Upserts the row for a valid tenant id.
    """
    raw = (obj.get("metadata") or {}).get("tenant_id") or obj.get("client_reference_id")
    if raw:
        try:
            tenant_id = UUID(str(raw))
        except ValueError:
            logger.warning("stripe_event_bad_tenant_metadata")
            return None
        ent = await session.get(Entitlement, tenant_id)
        if ent is None:
            ent = Entitlement(tenant_id=tenant_id)
            session.add(ent)
        return ent

    customer = obj.get("customer")
    if customer:
        return await session.scalar(
            select(Entitlement).where(Entitlement.stripe_customer_id == str(customer))
        )
    return None


def _period_dt(ts: Any) -> datetime | None:
    """Stripe unix timestamp -> aware UTC datetime (None passes through)."""
    return datetime.fromtimestamp(ts, tz=UTC) if ts else None


def _plan_id_from_metered_companion(cid: str) -> str | None:
    """If `cid` is a `{plan_id}_metered_patients` / `{plan_id}_metered_professionals` /
    `{plan_id}_metered_reminders` STRIPE_PRICE_MAP key whose stripped plan id is a real
    catalog plan, return that plan id; otherwise None. This is the "evidence of plan" a
    FULLY METERED subscription needs (CONTRACT_onboarding_v1.md §9): with no anchor/flat
    price item at all, a companion price is the ONLY signal `_state_from_subscription`
    has to resolve the plan. All three companions independently resolve to the SAME plan
    id, so assigning it from any one (or more) is idempotent. The SUPERSEDED single
    `{plan_id}_metered` key is deliberately NOT matched here — it keeps today's
    "ignored, no plan evidence" behavior (harmless: it isn't a catalog id, so it lands in
    neither branch below).
    """
    for suffix in (METERED_PATIENTS_SUFFIX, METERED_PROFESSIONALS_SUFFIX, METERED_REMINDERS_SUFFIX):
        if cid.endswith(suffix):
            candidate = cid[: -len(suffix)]
            if candidate in catalog.PLAN_IDS:
                return candidate
    return None


def _state_from_subscription(sub: dict[str, Any]) -> dict[str, Any] | None:
    """Derive the full entitlement state a Stripe subscription implies.

    Maps each item's price id back to a catalog id: exactly one plan + N add-ons
    (quantities scale an add-on's additive limit grants — `multi_professional` ×3 buys
    3 extra professionals). A FULLY METERED plan (CONTRACT_onboarding_v1.md §9 — no flat/
    anchor price, e.g. secretaria_basico) carries NO plan-price item at all: its
    `{plan_id}_metered_patients` / `{plan_id}_metered_professionals` /
    `{plan_id}_metered_reminders` companion items are the ONLY evidence of the plan
    (`_plan_id_from_metered_companion`); any one resolves it, and a companion item NEVER
    enters `addon_qty` (it is not an add-on). Returns None when no plan is recognized by
    either route (the caller then updates status/period only and logs — never guesses a
    plan).
    """
    plan_id: str | None = None
    addon_qty: dict[str, int] = {}
    for item in (sub.get("items") or {}).get("data") or []:
        price_id = (item.get("price") or {}).get("id")
        cid = catalog_id_for_price(price_id) if price_id else None
        if cid is None:
            logger.warning("stripe_unknown_price_ignored", price_id=price_id)
            continue
        metered_plan_id = _plan_id_from_metered_companion(cid)
        if metered_plan_id is not None:
            plan_id = metered_plan_id
            continue
        if cid in catalog.PLAN_IDS:
            plan_id = cid
        else:
            addon_qty[cid] = int(item.get("quantity") or 1)

    if plan_id is None:
        return None

    state = catalog.compute_entitlement_state(
        plan_id, {addon_id: qty > 0 for addon_id, qty in addon_qty.items()}
    )
    # Quantity scaling: compute_entitlement_state grants each active add-on once;
    # add the remaining (qty - 1) units of its additive limit grants.
    limits = dict(state["limits"])
    for addon_id, qty in addon_qty.items():
        addon = catalog.get_addon(addon_id)
        if addon is None or qty <= 1:
            continue
        for key, grant in addon.limit_grants.items():
            limits[key] = limits.get(key, 0) + (qty - 1) * grant
    state["limits"] = limits
    state["plan"] = plan_id
    return state


async def _create_subscription_for_signup(
    *,
    intent_id: UUID,
    tenant_id: UUID | None,
    selection: CheckoutSelection,
    stripe_customer_id: str,
    setup_intent_id: str,
) -> dict[str, Any]:
    """Create the REAL Stripe subscription behind a completed cold-signup SETUP session.

    The signup Checkout Session is `mode=setup`: it only collects and saves a card (no
    subscription, no trial, and therefore none of Stripe's own hosted "X-day free trial"
    banner, which cannot be suppressed while the trial lives inside the Checkout Session).
    The subscription is created HERE instead, server-side, invisible to the buyer, with the
    SAME `trial_period_days` the embedded trial used to carry — and creating it fires the
    very same `customer.subscription.created` event Stripe would have fired on its own, so
    every downstream state machine (entitlement recompute, `harden_charge`,
    `trial_will_end` auto-cancel) is untouched by this change.

    Shape mirrors `api/onboarding.py`'s test-window restart branch exactly (`items[i]` via
    `_append_subscription_items`, `default_payment_method`, `metadata[tenant_id]`, and
    `trial_period_days` as a TOP-LEVEL key — the `subscription_data[...]` nesting only
    applies to Checkout Sessions). The card comes from the completed SetupIntent's
    `payment_method`; Checkout already attached it to the customer (verified live against
    `GET /v1/payment_methods?customer=…&type=card`), so nothing needs attaching here.

    Returns the FULL subscription object, not just its id: the caller activates the
    entitlement with the `status`/period it reports (it comes back `trialing`, and the
    whole test-window mechanism gates on that — see
    `services.signup.provision_tenant_from_intent`'s `subscription_payload` docstring).
    """
    settings = get_settings()
    setup_intent = await _stripe_get(f"/v1/setup_intents/{setup_intent_id}")
    payment_method = setup_intent.get("payment_method")
    if isinstance(payment_method, dict):  # defensive: only if someone expands the field
        payment_method = payment_method.get("id")
    if not payment_method:
        # HARD failure, deliberately propagated (502 ⇒ the webhook does not ack ⇒ Stripe
        # redelivers). A completed setup session normally has this populated with no
        # `expand` at all (verified live), so the realistic cause is a SetupIntent still
        # `processing` — which settles on its own within Stripe's retry window. Creating
        # the subscription anyway would silently produce one with NO
        # `default_payment_method`: perfectly healthy-looking for the whole trial, then
        # unchargeable at the first invoice with no card on file and nobody watching.
        logger.error(
            "signup_setup_intent_without_payment_method",
            intent_id=str(intent_id),
            setup_intent_id=setup_intent_id,
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "setup_intent_without_payment_method"
        )

    data: dict[str, str] = {
        "customer": stripe_customer_id,
        "proration_behavior": "none",
        # Same metadata the old `subscription_data[metadata][signup_intent_id]` put on the
        # Checkout-created subscription — kept identical so nothing downstream loses the
        # link back to the signup intent.
        "metadata[signup_intent_id]": str(intent_id),
        "default_payment_method": str(payment_method),
    }
    if tenant_id is not None:
        # Parity with `api/onboarding.py`'s restart branch: `_entitlement_for_event`
        # resolves `metadata.tenant_id` FIRST, so every later `customer.subscription.*`
        # event for this subscription resolves without depending on the
        # `stripe_customer_id` column — which the signup handler is still in the middle of
        # writing when Stripe fires `customer.subscription.created` for this very POST.
        # (Registration always links a tenant; guarded because the rest of this path is
        # defensive about a null `tenant_id` too.)
        data["metadata[tenant_id]"] = str(tenant_id)
    _append_subscription_items(data, selection)
    if settings.STRIPE_TRIAL_PERIOD_DAYS > 0:
        data["trial_period_days"] = str(settings.STRIPE_TRIAL_PERIOD_DAYS)

    # DETERMINISTIC idempotency key — the SECOND line of defense, not the first. The
    # PRIMARY guard against minting a second subscription is local: `apply_stripe_event`'s
    # `processed_stripe_events` dedup row, plus the `intent.status == "completed"` check
    # the caller now performs BEFORE ever reaching this function. This key covers only the
    # narrow window neither of those can: our process dying after Stripe confirms the
    # subscription but before the commit that would have written both markers, which rolls
    # them back together and lets a redelivered event re-run this code from the top.
    # Stripe then returns the ORIGINAL object instead of creating a second one (verified
    # live: replaying the same key with identical params yields the same subscription, and
    # the customer still has exactly one). Note the key EXPIRES on Stripe's side after
    # ~24h while redeliveries continue for ~3 days — which is precisely why it cannot be
    # the primary guard and the caller's status check has to be.
    payload = await _stripe_post(
        "/v1/subscriptions", data, idempotency_key=f"signup-sub-{intent_id}"
    )
    logger.info(
        "signup_subscription_created",
        intent_id=str(intent_id),
        subscription_id=str(payload.get("id")),
        subscription_status=payload.get("status"),
    )
    return payload


def _fail_intent(intent: SignupIntent, reason: str, intent_id: UUID) -> None:
    """Mark a cold-signup intent permanently failed, WITHOUT raising and WITHOUT
    activating anything.

    Mirrors `services.signup.provision_tenant_from_intent`'s `tenant_missing` convention
    exactly (`status="failed"` + `failure_reason`, no exception): the webhook must still
    ack so Stripe stops redelivering an event that can never succeed, and
    `apply_stripe_event`'s own commit — the one that also writes the
    `processed_stripe_events` row — persists this state. Logged at ERROR, not WARNING:
    every reason that lands here is a paying buyer who did NOT get what they bought, which
    is an alert, not a note. `GET /public/onboarding-status` surfaces the row as
    `status: "failed"` (services/signup.py), so the browser stops spinning too.
    """
    intent.status = "failed"
    intent.failure_reason = reason
    logger.error("signup_activation_failed", intent_id=str(intent_id), reason=reason)


async def _apply_signup_intent_checkout(
    session: AsyncSession, obj: dict[str, Any]
) -> Tenant | None:
    """`checkout.session.completed` for a cold signup: CREATE the subscription, then
    ACTIVATE the tenant it implies.

    The session is `mode=setup` (card capture only), so the event carries a `setup_intent`
    and NO `subscription`: this function creates the real subscription itself first
    (`_create_subscription_for_signup` — a Stripe GET + POST, the documented exception to
    this module's "webhook applies are pure local-DB writes" rule), then hands the
    resulting subscription id AND payload to `services.signup.provision_tenant_from_intent`.
    Both Stripe calls happen BEFORE any local write, so a Stripe failure propagates out of
    the webhook with nothing half-committed and Stripe redelivers. An event that DOES carry
    a `subscription` (a subscription-mode session, i.e. anything predating this change or
    replayed from before it) keeps the old behavior verbatim.

    THE ORDER MATTERS: `intent.status == "completed"` is checked BEFORE anything is sent to
    Stripe, and an already-completed intent REUSES `intent.stripe_subscription_id` instead
    of re-creating. The local dedup (`processed_stripe_events` + this status check) is the
    PRIMARY idempotency guard; the deterministic `Idempotency-Key` inside
    `_create_subscription_for_signup` is a backstop for the sub-24h crash window only,
    because Stripe expires idempotency keys after ~24h while it keeps redelivering a
    failing webhook for ~3 days — a day-2 redelivery that re-POSTed would mint a SECOND,
    orphaned, double-billing subscription no local row points at.

    The tenant/user/inert-entitlement already exist (registration built them at the first
    card); provisioning only flips the entitlement to the purchased plan.

    Resolution: our own `metadata.signup_intent_id` (stamped at checkout,
    services/signup.create_checkout_session_for_intent), falling back to
    `client_reference_id`. An unresolvable/unknown id is logged and dropped — the event
    is still marked processed by the caller (a redelivery cannot do better).

    FAILURE HANDLING: a setup completion carrying no `customer`, or a permanent failure of
    the derive+create step (a 422/503 from `validate_selection` — e.g. `STRIPE_PRICE_MAP`
    was edited between checkout and webhook — which will not fix itself inside Stripe's
    retry window), marks the intent `failed` and ACKS. It never activates anything:
    handing out a full entitlement with no subscription behind it is silent, total,
    unmonitored revenue loss, and the buyer sees `status: "failed"` on
    `GET /public/onboarding-status` instead of an eternal spinner. A TRANSIENT Stripe
    failure (502) is re-raised instead, so the webhook 500s, Stripe redelivers, and the
    signup self-heals.

    Returns the freshly-ACTIVATED `Tenant` row so the caller can fire the
    secretaria-provisioning bridge post-commit — `None` when nothing was newly activated
    THIS call (unknown/unresolvable intent, a failed activation, or an already-completed
    intent replayed by a Stripe redelivery — the bridge must fire only once, not on every
    redelivery). The "newly this call" signal is the intent's status transition to
    "completed" (it is keyed on status, NOT `tenant_id`, which registration always sets).
    """
    # Local import: services.signup imports services.billing (validate_selection,
    # price_id_for, _stripe_post), so a module-level import here would cycle.
    from brain_api.services import signup as signup_service

    raw = (obj.get("metadata") or {}).get("signup_intent_id") or obj.get("client_reference_id")
    if not raw:
        logger.warning("stripe_signup_event_missing_intent_id")
        return None
    try:
        intent_id = UUID(str(raw))
    except ValueError:
        logger.warning("stripe_signup_event_bad_intent_id")
        return None

    intent = await session.get(SignupIntent, intent_id)
    if intent is None:
        logger.warning("stripe_signup_event_unknown_intent", intent_id=str(intent_id))
        return None

    stripe_customer_id = str(obj["customer"]) if obj.get("customer") else None
    stripe_subscription_id = str(obj["subscription"]) if obj.get("subscription") else None
    setup_intent_id = obj.get("setup_intent")
    subscription_payload: dict[str, Any] | None = None

    already_completed = intent.status == "completed"
    if already_completed:
        # A redelivery of an intent we already activated. NEVER re-POST to Stripe: the
        # subscription exists, its id is on the intent, and Stripe's idempotency key for it
        # may well have expired by now (see the docstring). Reuse, never re-create.
        stripe_subscription_id = intent.stripe_subscription_id or stripe_subscription_id
        logger.info("stripe_signup_event_replayed", intent_id=str(intent_id))
    elif stripe_subscription_id is None and setup_intent_id and stripe_customer_id:
        # Re-derive the purchase from the intent's own catalog_ids — the same derivation
        # `provision_tenant_from_intent` runs for entitlement state, and the same one
        # `create_checkout_session_for_intent` ran to fail fast at checkout time. The
        # duplication is pre-existing and deliberate: the intent, not the Stripe event, is
        # the source of truth for WHAT was bought.
        #
        # Two checkout sessions for ONE intent are reachable (nothing marks an intent as
        # "checkout started" — `create_checkout_session_for_intent` only refuses once the
        # intent has LEFT `pending_payment`, so an abandoned-and-retried checkout simply
        # overwrites `stripe_session_id`). If a buyer somehow completed BOTH, the two
        # sessions would carry DIFFERENT Customers and collide on this intent's single
        # idempotency key — Stripe answers that with a permanent "Keys for idempotent
        # requests can only be used with the same parameters" error (verified live). The
        # `already_completed` branch above absorbs that: the second event finds the intent
        # completed and never calls Stripe at all. Only a genuinely CONCURRENT pair of
        # completions could still race past it, which no cheap local guard fixes (it needs
        # a row lock on the intent) and which requires the buyer to pay twice in parallel.
        plan_id = signup_service._plan_id_of(intent.catalog_ids)
        addon_ids = [cid for cid in intent.catalog_ids if cid in catalog.ADDON_IDS]
        try:
            selection = validate_selection(plan_id, addon_ids)
            subscription_payload = await _create_subscription_for_signup(
                intent_id=intent.id,
                tenant_id=intent.tenant_id,
                selection=selection,
                stripe_customer_id=stripe_customer_id,
                setup_intent_id=str(setup_intent_id),
            )
        except HTTPException as exc:
            # Named, greppable ERROR — without this a failure here was an untraceable
            # 500 repeating for three days with nothing in it identifying the signup.
            logger.error(
                "signup_subscription_create_failed",
                intent_id=str(intent_id),
                plan_id=plan_id,
                upstream_status=exc.status_code,
                detail=str(exc.detail),
            )
            if exc.status_code == status.HTTP_502_BAD_GATEWAY:
                # TRANSIENT (Stripe unreachable/erroring, or a SetupIntent still
                # settling): propagate so the webhook does NOT ack and Stripe redelivers.
                # Nothing local has been written yet, so this self-heals cleanly.
                raise
            # PERMANENT (422 unknown plan/add-on, 503 price_not_configured or Stripe not
            # configured at all): redelivering for three days cannot fix a config problem,
            # so fail the intent visibly and ack.
            _fail_intent(intent, "subscription_create_failed", intent_id)
            return None
        stripe_subscription_id = str(subscription_payload["id"])
    elif stripe_subscription_id is None and setup_intent_id:
        # A setup session with no `customer`. With `customer_creation=always` on the
        # session (services/signup.py) this is now UNREACHABLE — which is exactly why it
        # must be LOUD rather than degrade: it would mean Stripe changed behavior or the
        # session was built without that parameter, and the old degrade path handed the
        # tenant a full entitlement (`status="active"`, products ON) with no subscription
        # and no customer — a working product, permanently free, with a 200 on the webhook
        # and nothing anywhere saying so. Fail the intent instead: nothing is activated,
        # the buyer sees `failed` on the onboarding poll, and the error names the intent.
        _fail_intent(intent, "stripe_customer_missing", intent_id)
        return None

    await signup_service.provision_tenant_from_intent(
        session,
        intent,
        stripe_customer_id=stripe_customer_id,
        stripe_subscription_id=stripe_subscription_id,
        subscription_payload=subscription_payload,
    )
    # Fire the bridge only when THIS call is the one that activated the intent (status went
    # completed just now). Already-completed (redelivery) or failed (tenant_missing) -> None.
    if already_completed or intent.status != "completed":
        return None
    return await session.get(Tenant, intent.tenant_id)


def _reset_markers_if_subscription_changed(ent: Entitlement, new_subscription_id: str) -> bool:
    """Reset `charge_hardened_at`/`cancel_scheduled_at` to `None` when a NEW (non-null,
    different) subscription id is about to replace an existing one on this entitlement.
    Returns whether it actually reset (a genuine subscription CHANGE, not a redelivery of
    the same id) — Task 2's callers use this to know whether to ALSO restart the tenant's
    Meta/WABA acceptance test window (`_restart_test_window` below): a brand-new
    subscription is a fresh test window, same rationale as the marker reset itself.

    Both markers describe the lifecycle of the SUBSCRIPTION they were applied to, not
    the tenant — a resubscription (cancel -> resubscribe) starts an entirely new trial
    lifecycle. Empirically confirmed bug this guards against: without this reset, a
    tenant's SECOND subscription would inherit the first one's `charge_hardened_at`/
    `cancel_scheduled_at`, permanently disabling both `harden_charge` (idempotency check
    short-circuits on the stale marker) and `trial_will_end` scheduling (same) for that
    tenant — the new trial would run out with NOTHING protecting it, and the tenant
    would simply get charged whether or not they ever activated. A redelivery of the
    SAME subscription id must NOT reset anything (checked via inequality, not presence).
    """
    if ent.stripe_subscription_id is not None and ent.stripe_subscription_id != new_subscription_id:
        ent.charge_hardened_at = None
        ent.cancel_scheduled_at = None
        return True
    return False


async def _restart_test_window(session: AsyncSession, tenant_id: UUID) -> None:
    """Restart a tenant's Meta/WABA acceptance test window (Task 2) alongside a genuine
    subscription-id change (`_reset_markers_if_subscription_changed` returning `True`): a
    brand-new subscription starts a fresh window, the same effect
    `POST /doctor/onboarding/test-window/restart` produces for a manual re-checkout.
    Defensive no-op if the tenant row is somehow missing — must never block the webhook.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:  # pragma: no cover - defensive; an entitlement always has a tenant row.
        logger.warning("test_window_restart_tenant_missing", tenant_id=str(tenant_id))
        return
    tenant.test_window_started_at = datetime.now(UTC)
    tenant.test_window_notified_at = None


async def apply_stripe_event(
    session: AsyncSession, event_id: str, event_type: str, obj: dict[str, Any]
) -> bool:
    """Apply one verified Stripe event to the local entitlement. Returns False for a
    duplicate (already-processed `event_id`). Commits marker + mutation TOGETHER.

    Handled types (everything else is marked processed and ignored):
    - checkout.session.completed        -> link stripe_customer_id / subscription_id
      (or, for a cold-signup checkout — `metadata.kind == "signup_intent"` — CREATE the
      subscription the `mode=setup` session's saved card implies and then ACTIVATE the
      already-registered tenant's inert entitlement, via `_apply_signup_intent_checkout`)
    - customer.subscription.created/updated -> full recompute (plan/addons/limits/
      products/status/period) from the subscription items via the catalog
    - customer.subscription.deleted     -> status=canceled, products OFF
    - customer.subscription.trial_will_end -> after a row-locked re-read + a LIVE Stripe
      GET verify the subscription is still genuinely trialing, schedule a Stripe
      `cancel_at` for a secretarIA-bearing plan that never activated (§13.6); no-op once
      hardened/already scheduled/not still trialing (locally or live)/plan doesn't
      enable secretarIA
    - invoice.payment_failed            -> status=past_due
    - invoice.paid                      -> past_due recovers to active
    """
    if await session.get(ProcessedStripeEvent, event_id) is not None:
        return False
    session.add(ProcessedStripeEvent(id=event_id, event_type=event_type))

    if (
        event_type == "checkout.session.completed"
        and (obj.get("metadata") or {}).get("kind") == "signup_intent"
    ):
        provisioned_tenant = await _apply_signup_intent_checkout(session, obj)
        await session.commit()
        logger.info("stripe_event_applied", event_type=event_type, kind="signup_intent")
        if provisioned_tenant is not None:
            # Best-effort, post-commit, fully self-contained try/except (never raises —
            # see its own docstring): a secretaria outage must never break this webhook.
            # Local import: services.onboarding_sync imports services.billing
            # (harden_charge), so a module-level import here would cycle.
            from brain_api.services import onboarding_sync

            await onboarding_sync.ensure_secretaria_provisioned(session, provisioned_tenant)
        return True

    ent = await _entitlement_for_event(session, obj)
    if ent is None:
        # Unresolvable tenant: mark processed (a redelivery cannot do better) + log.
        logger.warning("stripe_event_unresolved_tenant", event_type=event_type)
        await session.commit()
        return True

    if event_type == "checkout.session.completed":
        if obj.get("customer"):
            ent.stripe_customer_id = str(obj["customer"])
        if obj.get("subscription"):
            new_subscription_id = str(obj["subscription"])
            if _reset_markers_if_subscription_changed(ent, new_subscription_id):
                # Task 2: a genuine subscription-id change restarts the test window too.
                await _restart_test_window(session, ent.tenant_id)
            ent.stripe_subscription_id = new_subscription_id
        # Plan/status recompute rides the subscription.* events Stripe sends alongside.

    elif event_type in ("customer.subscription.created", "customer.subscription.updated"):
        if obj.get("customer"):
            ent.stripe_customer_id = str(obj["customer"])
        if obj.get("id"):
            new_subscription_id = str(obj["id"])
            if _reset_markers_if_subscription_changed(ent, new_subscription_id):
                # Task 2: a genuine subscription-id change restarts the test window too.
                await _restart_test_window(session, ent.tenant_id)
            ent.stripe_subscription_id = new_subscription_id
        ent.status = _STATUS_MAP.get(obj.get("status", ""), "inactive")
        ent.period_start = _period_dt(obj.get("current_period_start"))
        ent.period_end = _period_dt(obj.get("current_period_end"))
        state = _state_from_subscription(obj)
        if state is not None:
            ent.plan = state["plan"]
            ent.precheck_enabled = state["precheck_enabled"]
            ent.secretaria_enabled = state["secretaria_enabled"]
            # Whole-dict reassignment triggers JSON change tracking (no flag_modified).
            ent.addons = state["addons"]
            ent.limits = state["limits"]
        else:
            logger.warning("stripe_subscription_no_known_plan", event_type=event_type)

    elif event_type == "customer.subscription.deleted":
        # Billing-managed access ends with the subscription; an admin can still
        # manually re-enable via PATCH (§11) if commercially warranted.
        ent.status = "canceled"
        ent.precheck_enabled = False
        ent.secretaria_enabled = False

    elif event_type == "customer.subscription.trial_will_end":
        # Stripe fires this BOTH ~3 days before a subscription's scheduled trial end AND
        # immediately when a trial is ended right now (trial_end="now") — which is
        # exactly what harden_charge does on activation. Cheap short-circuit BEFORE any
        # row lock / Stripe call: an immediate-end firing reports a non-"trialing" status
        # right on the event's own subscription object, so most stale firings are
        # skipped here for free. This is NOT the authoritative check by itself anymore
        # (an unverified assumption about what Stripe embeds in the event payload) — the
        # locked re-read + live refetch below are.
        if obj.get("status") == "trialing":
            # Row-lock + fresh read: harden_charge (below) ALSO locks this same row
            # before writing its own markers. Locking here serializes the two critical
            # sections, so a trial_will_end redelivery racing harden_charge's own
            # trial_end="now" firing of THIS SAME event type can never be evaluated
            # against a pre-harden snapshot — it blocks until harden's commit lands, and
            # then sees charge_hardened_at already set. FOR UPDATE is a no-op on SQLite
            # (tests unaffected) and a real row lock on Postgres. Kept as a SEPARATE
            # name from `ent` (rather than reassigning it) so the outer `ent` used by
            # the function's final logging line stays valid even in the
            # near-impossible case this re-read finds no row.
            locked_ent = await session.get(
                Entitlement, ent.tenant_id, with_for_update=True, populate_existing=True
            )
            # Guard meanings (evaluated on the FRESH, locked state): `status` not
            # "trialing" -> nothing to schedule; `charge_hardened_at` set -> tenant
            # already activated/paying, never touch it; `cancel_scheduled_at` set -> a
            # cancel_at is already scheduled (idempotency) — re-issuing the identical
            # value would be harmless but the redundant Stripe call is skipped.
            if (
                locked_ent is not None
                and locked_ent.status == "trialing"
                and locked_ent.charge_hardened_at is None
                and locked_ent.cancel_scheduled_at is None
                and locked_ent.stripe_subscription_id
            ):
                # Coexistence-conditional billing (FIX 4) applies ONLY to plans that
                # enable the secretarIA product. A PreCheck-only plan has no WhatsApp/
                # Coexistence component and structurally can never reach 'ativo'
                # (harden_charge fires only from secretarIA onboarding) — without this
                # gate, a happily-paying PreCheck customer's trial would get auto-
                # cancelled. An unresolved plan (None) is treated the same way: fail
                # toward charging a possibly-legit subscription, never toward killing
                # one.
                plan = catalog.get_plan(locked_ent.plan)
                if plan is not None and plan.secretaria:
                    # Live-subscription verification: covers the residual hole where
                    # harden_charge's Stripe call SUCCEEDED but its own DB commit then
                    # FAILED (local markers stay None even though the trial already
                    # ended on Stripe's side). The live object is the authority here —
                    # not the locked-but-possibly-stale local row, and NOT anything the
                    # event payload claims. Require the live trial_end to be more than an
                    # hour out so a subscription that is trialing-but-about-to-end isn't
                    # raced the other way either.
                    live = await _stripe_get(
                        f"/v1/subscriptions/{locked_ent.stripe_subscription_id}"
                    )
                    live_trial_end = live.get("trial_end")
                    if (
                        live.get("status") == "trialing"
                        and live_trial_end
                        and live_trial_end > datetime.now(UTC).timestamp() + 3600
                    ):
                        # Deliberately NO try/except anywhere in this branch (unlike
                        # harden_charge's fail-soft): a failure here must PROPAGATE so
                        # the webhook 500s and Stripe redelivers on its own ~3-day retry
                        # schedule (matches this event's lead time before trial end).
                        # harden_charge has another trigger to retry on (the next
                        # config-status refresh); this handler has none — swallowing the
                        # error here would silently lose the cancellation forever.
                        await _stripe_post(
                            f"/v1/subscriptions/{locked_ent.stripe_subscription_id}",
                            {
                                "cancel_at": str(int(live_trial_end)),
                                "proration_behavior": "none",
                            },
                        )
                        locked_ent.cancel_scheduled_at = datetime.now(UTC)

    elif event_type == "invoice.payment_failed":
        ent.status = "past_due"

    elif event_type == "invoice.paid":
        if ent.status == "past_due":
            ent.status = "active"

    await session.commit()
    logger.info("stripe_event_applied", event_type=event_type, tenant_id=str(ent.tenant_id))
    return True


# --- Charge hardening (CONTRACT_onboarding_v1.md §8/§9; onboarding round) -----------------


async def harden_charge(session: AsyncSession, tenant: Tenant) -> bool:
    """End a still-trialing subscription's trial early once the tenant reaches 'ativo'.

    Rationale: the Stripe trial exists to cover onboarding friction; a tenant that has
    actually connected WhatsApp and finished configuration is no longer "still setting
    up" and should start being billed on its normal cadence rather than keep accruing
    the FULL trial window. Idempotent (`entitlements.charge_hardened_at` already set ->
    no-op, `False`) and fully fail-soft: any Stripe failure is logged and simply retried
    on the NEXT config-status refresh (`services/onboarding_sync.py::refresh_config_status`
    is its only caller today), never raised into that caller.

    The Stripe payload's `cancel_at: ""` unconditionally CLEARS any Stripe-side scheduled
    cancellation (a no-op when none was scheduled) — the counterpart of the
    `customer.subscription.trial_will_end` handler's `cancel_at` scheduling (§13.6): a
    tenant reaching 'ativo' here is precisely the activation that handler's
    `charge_hardened_at` guard exists to protect against a LATER trial_will_end
    redelivery, but if a cancellation was already scheduled with Stripe BEFORE
    activation landed, it must be cleared too. `ent.cancel_scheduled_at` is reset to
    `None` in the SAME commit that stamps `charge_hardened_at`.

    Row-locked read (`with_for_update`, a no-op on SQLite/real on Postgres): serializes
    against a concurrently-applying `trial_will_end` webhook event locking the SAME row
    (§13.6) — whichever of the two gets here first commits its marker before the
    other's locked re-read proceeds, so neither can ever act on a stale pre-commit
    snapshot of the other.
    """
    ent = await session.get(Entitlement, tenant.id, with_for_update=True, populate_existing=True)
    if ent is None or ent.charge_hardened_at is not None:
        return False
    if ent.status != "trialing" or not ent.stripe_subscription_id:
        return False
    try:
        await _stripe_post(
            f"/v1/subscriptions/{ent.stripe_subscription_id}",
            {"trial_end": "now", "proration_behavior": "none", "cancel_at": ""},
        )
    except HTTPException:
        logger.warning("harden_charge_failed", tenant_id=str(tenant.id))
        return False
    ent.charge_hardened_at = datetime.now(UTC)
    ent.cancel_scheduled_at = None
    await session.commit()
    logger.info("harden_charge_applied", tenant_id=str(tenant.id))
    return True
