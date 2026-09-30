"""Entitlement model — one row per tenant (stripe-billing-entitlements skill).

The runtime authority for "what is this tenant allowed to use, right now". Read
synchronously, in-process, before any gated work. Recomputed from Stripe webhooks in a
future round — there is NO Stripe call to answer a read.

Extended beyond the base skill shape with explicit product-access flags
(`precheck_enabled` / `secretaria_enabled`) which the portal uses to show/link products.
`addons` and `limits` carry the formalized keysets declared in `services/catalog.py`
(add-on id -> bool; limit key -> int), materialized by admin PATCH today and by the
Stripe webhook recompute in the billing round; `usage` stays a scaffold until metering.
Mutate JSON columns by whole-dict reassignment (or `flag_modified`), or SQLAlchemy won't
persist in-place edits.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class Entitlement(Base):
    """Per-tenant entitlement state. Primary-keyed by tenant_id (one row per tenant)."""

    __tablename__ = "entitlements"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )

    # --- Active products (the portal links these when true) ---
    precheck_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )
    secretaria_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )

    # --- Plan / subscription status ---
    # `plan` is the ANCHOR plan (TASK C, docs/CHECKPOINT_billing_add_product.md): the
    # secretarIA plan (`secretaria_basico` / `complete_clinic_combo`) whenever the clinic
    # has secretarIA, else the PreCheck tier, else `free`. Readers that need the PreCheck
    # tier of a clinic that has BOTH products use `catalog.precheck_plan_of(ent)`.
    plan: Mapped[str] = mapped_column(String(32), server_default="free", default="free")
    # The PreCheck tier (`precheck_start|basic|advanced`) WHEN the anchor `plan` is a
    # secretarIA plan without PreCheck. NULL when PreCheck comes from `plan` itself
    # (PreCheck-only, or the combo) or there is no PreCheck.
    precheck_plan: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Product families ("precheck" / "secretaria") switched on OUTSIDE Stripe — courtesy
    # coupon, admin PATCH, test clinic. Only a LIVE subscription that carries the family
    # converts it to "paid" (services.billing.apply_subscription_state); without this list
    # every later subscription event would recompute the row from that one subscription
    # alone and switch the courtesy product off. Reassign the whole list (JSON column).
    manual_products: Mapped[list] = mapped_column(JSON, server_default=text("'[]'"), default=list)
    status: Mapped[str] = mapped_column(
        String(32), server_default="inactive", default="inactive"
    )  # active | trialing | past_due | canceled | inactive

    # --- Scaffolds (MVP: empty; future billing recompute fills these) ---
    addons: Mapped[dict] = mapped_column(JSON, server_default=text("'{}'"), default=dict)
    limits: Mapped[dict] = mapped_column(JSON, server_default=text("'{}'"), default=dict)
    usage: Mapped[dict] = mapped_column(JSON, server_default=text("'{}'"), default=dict)

    # --- Billing period + Stripe linkage (scaffold; unused in current scope) ---
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Set once services.onboarding's harden_charge (B2) ends a trial early
    # (`trial_end=now`) after the tenant reaches `ativo`. Idempotency marker: a set value
    # means "already hardened, skip" — never re-applied.
    charge_hardened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when the trial_will_end handler schedules a Stripe cancel_at because the
    # tenant hadn't reached 'ativo' yet. Cleared by harden_charge if the tenant
    # activates afterward (the race this column exists to prevent: an already-
    # scheduled Stripe cancellation must not fire against a now-active, paying
    # subscription).
    cancel_scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
