"""Pydantic v2 schemas for the cold-signup vertical (register -> public checkout ->
webhook activation).

The client only ever names CATALOG ids (validated here against `services/catalog.py`) and
a Stripe Checkout Session id it already holds — never a price, an amount, or anything
that decides what it paid (the webhook recompute in `services/signup.
provision_tenant_from_intent` is the sole *entitlement-activation* writer on this path,
mirroring the stripe-billing-entitlements invariant `services/billing.py` documents).

Registration (`POST /public/signup-intents` -> `services.signup.register_signup`) now
also carries a real `password`: the lead becomes a full tenant + owner user + inert
entitlement the moment the FIRST card is submitted, so the visitor can log in normally
afterwards regardless of whether they ever finish the wizard or pay. The webhook only
flips the already-existing inert entitlement to the purchased plan.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from brain_api.schemas.auth import TokenResponse
from brain_api.services import catalog


def _validate_catalog_ids(catalog_ids: list[str]) -> None:
    """Shared catalog-selection rule for BOTH registration (`SignupIntentCreate`) and the
    later add-on update (`SignupIntentCatalogPatchIn`): every id must be known (a catalog
    plan or add-on) and the list must carry EXACTLY one assignable, non-free plan id.
    Raises `ValueError` (surfaces as a 422 via pydantic) on any violation.
    """
    known = catalog.PLAN_IDS | catalog.ADDON_IDS
    unknown = set(catalog_ids) - known
    if unknown:
        raise ValueError(f"unknown_catalog_ids:{sorted(unknown)}")

    plan_ids = [cid for cid in catalog_ids if cid in catalog.PLAN_IDS]
    if len(plan_ids) != 1:
        raise ValueError("signup requires exactly one plan catalog id")
    plan_id = plan_ids[0]
    if plan_id == catalog.PLAN_FREE or plan_id not in catalog.ASSIGNABLE_PLAN_IDS:
        raise ValueError(f"unknown_or_unassignable_plan:{plan_id}")


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
    """Body for `POST /public/signup-intents` — the FIRST-card lead + selection + password.

    Submitting this now REGISTERS the account (tenant + owner user + inert entitlement +
    linked intent) and returns a real session — no longer a mere pre-payment placeholder.
    `password` is the visitor's chosen password (same composition policy as
    `schemas.auth.SetPasswordIn` / `schemas.admin.AdminUserCreateIn`: 8-72 chars, at least
    one letter and one digit) so the owner can log back in from any device afterwards,
    instead of the old random-never-communicated one.

    `catalog_ids` must contain EXACTLY one assignable, non-free plan id plus any number
    of known add-on ids (unknown ids, zero or multiple plan ids, or the reserved/free
    plan are all 422). `website` is a HONEYPOT (demo.py pattern): a bot fills it, a real
    browser leaves it empty; it is NEVER persisted. `intake` stays OPTIONAL here — the
    later wizard steps attach it via the authenticated `POST /doctor/onboarding/intake`
    once the visitor is logged in.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=255)
    clinic_name: str = Field(min_length=1, max_length=255)
    email: EmailStr = Field(max_length=320)
    whatsapp_phone: str = Field(min_length=1, max_length=32)
    # bcrypt truncates at 72 bytes; reject longer (422) rather than silently truncate.
    password: str = Field(min_length=8, max_length=72)
    catalog_ids: list[str] = Field(min_length=1, max_length=16)
    # Optional eligibility-calibration answers (§7); omitted -> None (back-compat). Usually
    # attached LATER via POST /doctor/onboarding/intake, but accepted here too.
    intake: IntakeIn | None = None
    #: Slug do template de especialidade do PreCheck escolhido na vitrine (/comecar).
    #: Opcional: compras sem PreCheck não mandam, e sem ele o bridge cai em
    #: "clinica-geral" (onboarding_sync.DEFAULT_PRECHECK_TEMPLATE).
    precheck_template_slug: str | None = Field(
        default=None, min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$"
    )
    # HONEYPOT (anti-spam). Never persisted — the API layer accept-and-drops silently.
    website: str | None = None

    @field_validator("name", "clinic_name")
    @classmethod
    def _trim(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("password")
    @classmethod
    def _password_policy(cls, v: str) -> str:
        """Same rule as SetPasswordIn/AdminUserCreateIn: reject a purely-numeric or
        purely-alphabetic password (422) so the owner's credential is never trivial."""
        if not any(c.isalpha() for c in v) or not any(c.isdigit() for c in v):
            raise ValueError("password must contain at least one letter and one digit")
        return v

    @model_validator(mode="after")
    def _validate_catalog_selection(self) -> "SignupIntentCreate":
        _validate_catalog_ids(self.catalog_ids)
        return self


class SignupRegisterOut(BaseModel):
    """`POST /public/signup-intents` response — the created intent id PLUS a real session.

    The visitor is registered and logged in the moment the first card is submitted: the
    frontend calls `saveSession(session)` immediately and treats the flow exactly like a
    normal login. `intent_id` drives the subsequent `POST /public/checkout-sessions` call.
    `session` is the SAME `TokenResponse` shape `POST /auth/token` (login) returns.
    """

    intent_id: UUID
    session: TokenResponse


class SignupIntentCatalogPatchIn(BaseModel):
    """Body for `PATCH /public/signup-intents/{intent_id}` — replace the catalog
    selection on a still-`pending_payment` intent (the pre-checkout add-on picker calls
    this right before requesting the Checkout Session).

    Same SHAPE rule as registration (`_validate_catalog_ids`): exactly one known,
    assignable, non-free plan id plus any number of known add-on ids. Two further rules
    that need the CURRENT intent row / the live price map — the plan itself may not
    actually change, and every add-on must have a configured Stripe price — cannot be
    checked here (a schema has no DB/settings access) and are enforced by
    `services.signup.update_intent_catalog` instead.
    """

    model_config = ConfigDict(extra="forbid")

    catalog_ids: list[str] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def _validate_catalog_selection(self) -> "SignupIntentCatalogPatchIn":
        _validate_catalog_ids(self.catalog_ids)
        return self


class SignupIntentCatalogOut(BaseModel):
    """`PATCH /public/signup-intents/{intent_id}` response — the persisted selection,
    normalized by `services.signup.update_intent_catalog` (plan id first, add-ons
    deduped in the order given)."""

    intent_id: UUID
    catalog_ids: list[str]
    status: str


class CheckoutSessionCreate(BaseModel):
    """Body for `POST /public/checkout-sessions` — open Checkout for a pending intent."""

    model_config = ConfigDict(extra="forbid")

    intent_id: UUID


class CheckoutSessionOut(BaseModel):
    """The Stripe-hosted Checkout page to redirect the browser to."""

    checkout_url: str


class AddonAvailabilityOut(BaseModel):
    """One entry of `CheckoutConfigOut.addons` — whether THIS environment has a Stripe
    price configured for the add-on. Every `catalog.ADDON_IDS` entry gets one of these
    regardless of `available` (the picker needs to know about an unavailable add-on too,
    to render it disabled rather than simply omit it)."""

    id: str
    available: bool


class CheckoutConfigOut(BaseModel):
    """`GET /public/checkout-config` response — public, non-secret checkout-funnel
    config. `trial_period_days`: the funnel's pre-checkout disclosure copy must quote
    the REAL deployed `STRIPE_TRIAL_PERIOD_DAYS` rather than hardcoding a second source
    of truth that could silently drift from what Checkout actually applies. `addons`
    (corrections round, 2026-07-22): one entry per `catalog.ADDON_IDS` id, stable
    alphabetical order, so the pre-checkout add-on picker can hide/disable an add-on
    that is sellable in the catalog but has no Stripe price wired in THIS environment
    yet — the same `billing.price_id_for` lookup `PATCH /public/signup-intents/{id}`
    uses for its own `addon_not_available` guard.
    """

    trial_period_days: int
    addons: list[AddonAvailabilityOut]


class OnboardingStatusOut(BaseModel):
    """`GET /public/onboarding-status` response.

    `products`/`onboarding_token` are `null` while `status != "ready"`. Once ready,
    `onboarding_token` carries a FRESH plaintext token each poll until it has been
    redeemed (`POST /auth/exchange-onboarding-token`), after which it stays `null`.
    """

    status: Literal["pending", "ready", "failed"]
    products: dict[str, bool] | None = None
    onboarding_token: str | None = None
