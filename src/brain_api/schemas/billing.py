"""Pydantic v2 schemas for the billing vertical (stripe-billing-entitlements).

The client only ever names CATALOG ids (validated server-side against the catalog +
price map) and only ever receives a redirect URL — no price, amount, or Stripe object
crosses this boundary, and nothing the client sends decides what it paid (the webhook
recompute is the sole entitlement writer on the billing path).
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from brain_api.schemas.entitlement import EntitlementOut


class CheckoutRequest(BaseModel):
    """`POST /billing/checkout` body — buy a catalog plan (optionally with add-ons)."""

    model_config = ConfigDict(extra="forbid")

    plan: str = Field(min_length=1, max_length=32)
    addons: list[str] = Field(default_factory=list, max_length=16)

    # Where the buyer lands after paying. NEVER a URL: a small allowlisted keyword
    # (`services.billing.RETURN_TO_ALLOWLIST`, currently "console" = the Brain-Message
    # portal). The success URL itself is built server-side, so nothing the client sends can
    # become a redirect target. `max_length` only bounds the payload; the allowlist decides.
    return_to: str | None = Field(default=None, max_length=32)


class CheckoutSessionOut(BaseModel):
    """The Stripe-hosted Checkout page to redirect the browser to."""

    url: str


class PortalSessionOut(BaseModel):
    """The Stripe-hosted Billing Portal page to redirect the browser to."""

    url: str


# --- PreCheck billing (precheck-billing round) ------------------------------


class PrecheckTopupIn(BaseModel):
    """`POST /billing/precheck/topup` body — how many avulso consultations to buy.

    The top-up Stripe Price is per UNIT, so this quantity is both what Stripe charges
    (`quantity x unit price`) and what the webhook grants. `gt=0` here is only a sanity
    floor on the wire; the REAL bounds (`Settings.PRECHECK_TOPUP_MIN_QUANTITY` /
    `PRECHECK_TOPUP_MAX_QUANTITY`) are enforced in `services.billing.
    create_precheck_topup_checkout_session`, which answers 422 `quantity_below_minimum` /
    `quantity_above_maximum` — they are operator-tunable env values, so they cannot live
    in a Field constraint evaluated at import time.
    """

    model_config = ConfigDict(extra="forbid")

    quantity: int = Field(gt=0, le=1_000_000)


class PrecheckUpgradeIn(BaseModel):
    """`POST /billing/precheck/upgrade` body — swap to another PreCheck tier.

    `plan` names a CATALOG id (validated server-side against `catalog.
    PRECHECK_TIER_PLAN_IDS` by
    `services.billing.upgrade_precheck_plan`, same "client only ever names catalog ids"
    rule as `CheckoutRequest.plan` above).
    """

    model_config = ConfigDict(extra="forbid")

    plan: str = Field(min_length=1, max_length=32)


class PrecheckSpendOut(BaseModel):
    """Top-up spend inside the CURRENT quota window (`GET /billing/precheck/usage`)."""

    topup_cents: int
    topup_count: int
    currency: str | None = None


class PrecheckUsageOut(BaseModel):
    """`GET /billing/precheck/usage` response, and `POST /billing/precheck/upgrade`'s
    confirmation payload (same shape — the upgrade endpoint returns the fresh state it
    just produced instead of making the caller immediately re-fetch it).

    Always 200: zeros/false when the tenant's resolved plan isn't PreCheck-enabled at all
    (`precheck_enabled=false`, `enforced=false`) — the frontend hides the PreCheck usage
    section in that case rather than treating it as an error.
    """

    plan: str
    plan_name: str
    precheck_enabled: bool
    enforced: bool
    quota: int
    used: int
    remaining: int
    topup_credits: int
    topup_expires_at: datetime | None = None
    window_start: datetime
    window_end: datetime
    spend: PrecheckSpendOut


# --- Add the OTHER product to the existing subscription (TASK C) -------------------------


class AddProductChargeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    """What the addition costs. Preview: `amount_due_now_cents` = charged immediately
    (proration), `next_invoice_*` = the next renewal (metered secretarIA usage not included).
    Executed: `amount_due_now_cents` = what the update's invoice actually collected."""

    currency: str
    amount_due_now_cents: int | None = None
    next_invoice_cents: int | None = None
    next_invoice_date: str | None = None


class AddProductIn(BaseModel):
    """`POST /billing/add-product` body. The client names CATALOG ids only — never a
    subscription, customer or tenant (`extra="forbid"`; the tenant comes from the JWT).

    `plan` is required for `product=precheck` (one of the three tiers) and forbidden for
    `secretaria` (one plan) — enforced in the service, where the catalog lives. `confirm=false`
    is a read-only preview; `confirm=true` executes and needs the `Idempotency-Key` header.
    """

    model_config = ConfigDict(extra="forbid")

    product: Literal["precheck", "secretaria"]
    plan: str | None = Field(default=None, min_length=1, max_length=32)
    addons: list[str] = Field(default_factory=list, max_length=16)
    confirm: bool = False
    expected_charge: AddProductChargeOut | None = None
    quote_token: str | None = Field(default=None, max_length=4096)

    # Where the buyer lands after the addition: an allowlisted keyword (`"console"` = the
    # Brain-Message portal), never a URL — `services.billing.RETURN_TO_ALLOWLIST` decides.
    return_to: str | None = Field(default=None, max_length=32)


class AddProductOut(BaseModel):
    """`POST /billing/add-product` response. `entitlement` only on `added`/`already_present`."""

    status: Literal["preview", "added", "already_present"]
    product: str
    charge: AddProductChargeOut | None = None
    entitlement: EntitlementOut | None = None

    # `origem=console[&produto=precheck]` for the brain-frontend's /checkout/sucesso; built
    # server-side, only on `added`/`already_present` and only when `return_to` was sent.
    return_query: str | None = None
    quote_token: str | None = None
