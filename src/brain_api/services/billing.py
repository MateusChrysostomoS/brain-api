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
from brain_api.models import Entitlement, ProcessedStripeEvent
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


@lru_cache(maxsize=8)
def _parse_price_map(raw: str) -> dict[str, str]:
    """Parse STRIPE_PRICE_MAP (keyed by the raw string so a settings monkeypatch in
    tests gets its own cache slot). Unknown catalog ids are rejected loudly at parse
    time — a typo'd map must not silently unsell a product."""
    try:
        mapping = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"STRIPE_PRICE_MAP is not valid JSON: {exc}") from exc
    known = catalog.PLAN_IDS | catalog.ADDON_IDS
    unknown = set(mapping) - known
    if unknown:
        raise ValueError(f"STRIPE_PRICE_MAP has unknown catalog ids: {sorted(unknown)}")
    return {str(k): str(v) for k, v in mapping.items()}


def price_id_for(catalog_id: str) -> str | None:
    """The Stripe price id selling a catalog plan/add-on in THIS environment."""
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
    """
    plan = catalog.get_plan(plan_id)
    if plan is None or plan.id not in catalog.ASSIGNABLE_PLAN_IDS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown_or_unassignable_plan:{plan_id}"
        )
    addons: list[str] = []
    for addon_id in addon_ids or []:
        if addon_id not in catalog.ADDON_IDS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown_addon:{addon_id}"
            )
        if addon_id not in plan.included_addons and addon_id not in addons:
            addons.append(addon_id)

    for cid in (plan.id, *addons):
        if price_id_for(cid) is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"price_not_configured:{cid}"
            )
    return CheckoutSelection(plan_id=plan.id, addon_ids=tuple(addons))


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
        "metadata[tenant_id]": str(tenant_id),
        "subscription_data[metadata][tenant_id]": str(tenant_id),
    }
    for i, cid in enumerate((selection.plan_id, *selection.addon_ids)):
        data[f"line_items[{i}][price]"] = price_id_for(cid) or ""
        data[f"line_items[{i}][quantity]"] = "1"
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


async def _entitlement_for_event(
    session: AsyncSession, obj: dict[str, Any]
) -> Entitlement | None:
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


def _state_from_subscription(sub: dict[str, Any]) -> dict[str, Any] | None:
    """Derive the full entitlement state a Stripe subscription implies.

    Maps each item's price id back to a catalog id: exactly one plan + N add-ons
    (quantities scale an add-on's additive limit grants — `multi_professional` ×3 buys
    3 extra professionals). Returns None when no plan price is recognized (the caller
    then updates status/period only and logs — never guesses a plan).
    """
    plan_id: str | None = None
    addon_qty: dict[str, int] = {}
    for item in (sub.get("items") or {}).get("data") or []:
        price_id = (item.get("price") or {}).get("id")
        cid = catalog_id_for_price(price_id) if price_id else None
        if cid is None:
            logger.warning("stripe_unknown_price_ignored", price_id=price_id)
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


async def apply_stripe_event(
    session: AsyncSession, event_id: str, event_type: str, obj: dict[str, Any]
) -> bool:
    """Apply one verified Stripe event to the local entitlement. Returns False for a
    duplicate (already-processed `event_id`). Commits marker + mutation TOGETHER.

    Handled types (everything else is marked processed and ignored):
    - checkout.session.completed        -> link stripe_customer_id / subscription_id
    - customer.subscription.created/updated -> full recompute (plan/addons/limits/
      products/status/period) from the subscription items via the catalog
    - customer.subscription.deleted     -> status=canceled, products OFF
    - invoice.payment_failed            -> status=past_due
    - invoice.paid                      -> past_due recovers to active
    """
    if await session.get(ProcessedStripeEvent, event_id) is not None:
        return False
    session.add(ProcessedStripeEvent(id=event_id, event_type=event_type))

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
            ent.stripe_subscription_id = str(obj["subscription"])
        # Plan/status recompute rides the subscription.* events Stripe sends alongside.

    elif event_type in ("customer.subscription.created", "customer.subscription.updated"):
        if obj.get("customer"):
            ent.stripe_customer_id = str(obj["customer"])
        if obj.get("id"):
            ent.stripe_subscription_id = str(obj["id"])
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

    elif event_type == "invoice.payment_failed":
        ent.status = "past_due"

    elif event_type == "invoice.paid":
        if ent.status == "past_due":
            ent.status = "active"

    await session.commit()
    logger.info("stripe_event_applied", event_type=event_type, tenant_id=str(ent.tenant_id))
    return True
