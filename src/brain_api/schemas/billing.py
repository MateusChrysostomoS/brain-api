"""Pydantic v2 schemas for the billing vertical (stripe-billing-entitlements).

The client only ever names CATALOG ids (validated server-side against the catalog +
price map) and only ever receives a redirect URL — no price, amount, or Stripe object
crosses this boundary, and nothing the client sends decides what it paid (the webhook
recompute is the sole entitlement writer on the billing path).
"""

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
