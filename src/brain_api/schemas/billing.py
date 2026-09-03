"""Pydantic v2 schemas for the billing vertical (stripe-billing-entitlements).

The client only ever names CATALOG ids (validated server-side against the catalog +
price map) and only ever receives a redirect URL — no price, amount, or Stripe object
crosses this boundary, and nothing the client sends decides what it paid (the webhook
recompute is the sole entitlement writer on the billing path).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CheckoutRequest(BaseModel):
    """`POST /billing/checkout` body — buy a catalog plan (optionally with add-ons)."""

    model_config = ConfigDict(extra="forbid")

    plan: str = Field(min_length=1, max_length=32)
    addons: list[str] = Field(default_factory=list, max_length=16)


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
