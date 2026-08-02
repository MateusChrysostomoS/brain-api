"""PreCheck top-up credit ledger — avulso consultations purchased on top of a PreCheck
plan's monthly quota (precheck-billing round).

Per stripe-billing-entitlements: PreCheck billing is FLAT-PRICE-plus-quota, never
metered — a top-up is a one-off `mode=payment` Stripe Checkout Session
(services/billing.py::create_precheck_topup_checkout_session) priced PER CONSULTATION,
and this table is the LOCAL ledger of what was actually paid for and granted (our ledger,
not Stripe, answers "how many credits does this tenant have" —
services/precheck_billing.py::usage_summary). One row per purchase; a tenant may hold
several unexpired rows at once.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class PrecheckTopupCredit(Base):
    """One granted PreCheck avulso purchase (`checkout.session.completed`,
    `metadata.kind == "precheck_topup"` — services.billing._apply_precheck_topup_checkout).
    """

    __tablename__ = "precheck_topup_credits"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # Consultations granted by this purchase — the QUANTITY the doctor chose, stamped
    # into the Checkout Session's `metadata.quantity` at purchase time (and billed as
    # that same line-item quantity), so what is granted is always exactly what was paid
    # for, whatever the current purchase bounds happen to be.
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    # Stripe's own `amount_total` / `currency` off the completed Checkout Session, kept
    # for the tenant-facing spend summary (GET /billing/precheck/usage). Nullable: a
    # defensively-handled malformed/replayed event could in principle omit them.
    amount_total_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Natural idempotency key for this grant (belt-and-braces alongside
    # `processed_stripe_events`, services/billing.py) — one Checkout Session can only
    # ever grant exactly one credit row; a duplicate insert attempt is caught as a
    # unique-constraint violation and treated as a no-op replay.
    stripe_checkout_session_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Credits expire at the END of the quota window they were purchased in
    # (services/precheck_billing.py::quota_window) — never carried over past a billing
    # cycle / calendar month, so a tenant cannot stockpile credits indefinitely.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
