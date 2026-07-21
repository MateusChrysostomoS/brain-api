"""Pydantic v2 schemas for the cold-signup vertical (public checkout -> auto-provisioning).

The client only ever names CATALOG ids (validated here against `services/catalog.py`) and
a Stripe Checkout Session id it already holds — never a price, an amount, or anything
that decides what it paid (the webhook recompute in `services/signup.
provision_tenant_from_intent` is the sole entitlement writer on this path, mirroring the
stripe-billing-entitlements invariant `services/billing.py` documents).
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from brain_api.services import catalog


class IntakeIn(BaseModel):
    """Pre-checkout intake answers (CONTRACT_onboarding_v1.md §7).

    Stored verbatim on `signup_intents.intake` and consumed once, at provisioning, by
    `services.onboarding.provision_defaults` (-> `derive_initial_state` /
    `initial_next_retry_at`) to seed the tenant's initial onboarding state. The whole
    `intake` object is OPTIONAL on `SignupIntentCreate` (back-compat: omitted -> `None`,
    treated the same as "no signal" by `derive_initial_state`); when the client does send
    it, all three answers are required together.
    """

    model_config = ConfigDict(extra="forbid")

    whatsapp_usage: Literal["business_7d_plus", "business_recent", "none"]
    prior_api: Literal["yes", "no", "unknown"]
    fb_page: Literal["yes_admin", "yes_unknown_admin", "no"]


class SignupIntentCreate(BaseModel):
    """Body for `POST /public/signup-intents` — the pre-checkout lead + selection.

    `catalog_ids` must contain EXACTLY one assignable, non-free plan id plus any number
    of known add-on ids (unknown ids, zero or multiple plan ids, or the reserved/free
    plan are all 422). `website` is a HONEYPOT (demo.py pattern): a bot fills it, a real
    browser leaves it empty; it is NEVER persisted.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=255)
    clinic_name: str = Field(min_length=1, max_length=255)
    email: EmailStr = Field(max_length=320)
    whatsapp_phone: str = Field(min_length=1, max_length=32)
    catalog_ids: list[str] = Field(min_length=1, max_length=16)
    # Optional eligibility-calibration answers (§7); omitted -> None (back-compat).
    intake: IntakeIn | None = None
    # HONEYPOT (anti-spam). Never persisted — the API layer accept-and-drops silently.
    website: str | None = None

    @field_validator("name", "clinic_name")
    @classmethod
    def _trim(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @model_validator(mode="after")
    def _validate_catalog_selection(self) -> "SignupIntentCreate":
        known = catalog.PLAN_IDS | catalog.ADDON_IDS
        unknown = set(self.catalog_ids) - known
        if unknown:
            raise ValueError(f"unknown_catalog_ids:{sorted(unknown)}")

        plan_ids = [cid for cid in self.catalog_ids if cid in catalog.PLAN_IDS]
        if len(plan_ids) != 1:
            raise ValueError("signup requires exactly one plan catalog id")
        plan_id = plan_ids[0]
        if plan_id == catalog.PLAN_FREE or plan_id not in catalog.ASSIGNABLE_PLAN_IDS:
            raise ValueError(f"unknown_or_unassignable_plan:{plan_id}")
        return self


class SignupIntentOut(BaseModel):
    """`POST /public/signup-intents` response — just the id to drive the next step."""

    intent_id: UUID


class CheckoutSessionCreate(BaseModel):
    """Body for `POST /public/checkout-sessions` — open Checkout for a pending intent."""

    model_config = ConfigDict(extra="forbid")

    intent_id: UUID


class CheckoutSessionOut(BaseModel):
    """The Stripe-hosted Checkout page to redirect the browser to."""

    checkout_url: str


class CheckoutConfigOut(BaseModel):
    """`GET /public/checkout-config` response — public, non-secret checkout-funnel
    config. Today just the trial length: the funnel's pre-checkout disclosure copy must
    quote the REAL deployed `STRIPE_TRIAL_PERIOD_DAYS` rather than hardcoding a second
    source of truth that could silently drift from what Checkout actually applies.
    """

    trial_period_days: int


class OnboardingStatusOut(BaseModel):
    """`GET /public/onboarding-status` response.

    `products`/`onboarding_token` are `null` while `status != "ready"`. Once ready,
    `onboarding_token` carries a FRESH plaintext token each poll until it has been
    redeemed (`POST /auth/exchange-onboarding-token`), after which it stays `null`.
    """

    status: Literal["pending", "ready", "failed"]
    products: dict[str, bool] | None = None
    onboarding_token: str | None = None
