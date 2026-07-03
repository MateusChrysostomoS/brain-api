"""Stripe webhook idempotency ledger (stripe-billing-entitlements skill).

Stripe redelivers webhook events; without dedupe an entitlement recompute (or a future
usage increment) applies twice. One row per PROCESSED `event.id` — the insert happens in
the SAME transaction as the entitlement mutation, so a failed apply rolls the marker
back too and the redelivery legitimately reprocesses. Mirrors secretarIA's
`ProcessedEvent` pattern.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class ProcessedStripeEvent(Base):
    """A Stripe event id we already applied. PK = the event id (natural dedupe key)."""

    __tablename__ = "processed_stripe_events"

    # Stripe event ids are "evt_..." strings, well under 255.
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
