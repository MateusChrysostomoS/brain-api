"""Usage-event ledger — one row per recorded internal usage event (metering).

Per stripe-billing-entitlements: this is the METERING leg (our local ledger), separate
from entitlements (the runtime authority) and billing (Stripe). No Stripe call happens
anywhere in this module or its write path — see `services/usage.py`.

PK is the CALLER's own idempotency key (e.g. "reminder:24h:<appointment_id>"), not a
generated id: the ledger's job is to answer "have I already applied this event" via a
single `session.get`, mirroring `ProcessedStripeEvent`'s natural-key dedupe pattern.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class UsageEvent(Base):
    """A single applied usage event. PK = the caller's idempotency key (natural dedupe)."""

    __tablename__ = "usage_events"
    __table_args__ = (
        # Backs services/precheck_billing.py::usage_summary's window SUM query (tenant +
        # feature + a created_at RANGE scan) — the exact access pattern every PreCheck
        # quota check performs, potentially on every consultation attempt (precheck-
        # billing round; migration 0010_precheck_billing).
        Index("ix_usage_events_tenant_feature_created", "tenant_id", "feature", "created_at"),
    )

    # Caller-supplied idempotency key, e.g. "reminder:24h:<appointment_id>".
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    feature: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
