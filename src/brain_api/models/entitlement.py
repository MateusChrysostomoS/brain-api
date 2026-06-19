"""Entitlement model — one row per tenant (stripe-billing-entitlements skill).

The runtime authority for "what is this tenant allowed to use, right now". Read
synchronously, in-process, before any gated work. Recomputed from Stripe webhooks in a
future round — there is NO Stripe call to answer a read.

Extended beyond the base skill shape with explicit product-access flags
(`precheck_enabled` / `secretaria_enabled`) which the portal uses to show/link products.
`addons` / `limits` / `usage` are JSON scaffolds (empty for the MVP); mutate them with
`flag_modified` when the billing recompute lands, or SQLAlchemy won't persist in-place
JSON edits.
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
    plan: Mapped[str] = mapped_column(String(32), server_default="free", default="free")
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

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
