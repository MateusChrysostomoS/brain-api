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

The webhook apply functions are pure local-DB writes: the event payload (subscription
items, status, period) is the input; there is NO Stripe API call while applying. The
webhook route verifies the signature (pure HMAC) and dedupes on `event.id`
(`processed_stripe_events`, same transaction as the mutation). brain-api deliberately
has no Redis/arq (CONTRACTS.md §5), so the apply runs in-request — acceptable because
it is a handful of local writes, not network work; the skill's "enqueue" guidance
targets recomputes that call out.

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


async def _stripe_post(path: str, data: dict[str, str]) -> dict[str, Any]:
    """Form-encoded POST to the Stripe API. 503 when unconfigured, 502 on failure.

    Stripe's error body is logged at WARNING without the request payload (it can carry
    ids but no secrets); the secret key is only ever in the auth tuple, never logged.
    """
    settings = get_settings()
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "billing_not_configured")
    try:
        async with httpx.AsyncClient(
            base_url=settings.STRIPE_API_BASE, timeout=settings.STRIPE_TIMEOUT_SECONDS
        ) as client:
            resp = await client.post(path, data=data, auth=(settings.STRIPE_SECRET_KEY, ""))
    except httpx.RequestError as exc:
        logger.warning("stripe_unreachable", path=path)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "stripe_unavailable") from exc
    if resp.status_code >= 400:
        logger.warning("stripe_api_error", path=path, upstream_status=resp.status_code)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "stripe_error")
    return resp.json()


async def _stripe_get(path: str) -> dict[str, Any]:
    """GET counterpart of `_stripe_post` (same 503/502 error mapping). Used ONLY by the
    `customer.subscription.trial_will_end` handler's live-subscription verification
    (§13.6) — every other Stripe touchpoint in this module is a POST (checkout, portal,
    cancel scheduling, charge hardening); this is the one read of Stripe's own state,
    justified because the local `entitlements` row can lag behind what Stripe actually
    did (see the handler's own comments).
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
        logger.warning("stripe_api_error", path=path, upstream_status=resp.status_code)
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


async def _apply_signup_intent_checkout(
    session: AsyncSession, obj: dict[str, Any]
) -> Tenant | None:
    """`checkout.session.completed` for a cold signup: ACTIVATE the tenant it implies.

    The tenant/user/inert-entitlement already exist (registration built them at the first
    card); this only flips the entitlement to the purchased plan via
    `services.signup.provision_tenant_from_intent`.

    Resolution: our own `metadata.signup_intent_id` (stamped at checkout,
    services/signup.create_checkout_session_for_intent), falling back to
    `client_reference_id`. An unresolvable/unknown id is logged and dropped — the event
    is still marked processed by the caller (a redelivery cannot do better).

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

    already_completed = intent.status == "completed"
    await signup_service.provision_tenant_from_intent(
        session,
        intent,
        stripe_customer_id=str(obj["customer"]) if obj.get("customer") else None,
        stripe_subscription_id=str(obj["subscription"]) if obj.get("subscription") else None,
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
      (or, for a cold-signup checkout — `metadata.kind == "signup_intent"` — ACTIVATE the
      already-registered tenant's inert entitlement via `_apply_signup_intent_checkout`)
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
