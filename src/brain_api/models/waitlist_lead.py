"""WaitlistLead model — "avise-me quando lançar" capture for the pre-launch gate.

Isolated lead capture, exactly like `DemoRequest`: it does NOT reference tenants /
users / entitlements, never creates a tenant and never touches billing. It exists
only because the pricing page's buy buttons are gated shut before launch (see the
frontend's `PRODUCT_LAUNCHED` flag) and the visitor who wanted to buy is worth
remembering.

`email` is UNIQUE (stored lowercased by the service) so a visitor who clicks two
different plans, or comes back a week later, stays ONE row — the endpoint is
idempotent per email. `created_at` therefore means FIRST seen and is never
rewritten; `plan_hint` is refreshed to the most recent click, which is the more
useful signal for who to call first.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class WaitlistLead(Base):
    """A visitor who asked to be notified when the product goes on sale."""

    __tablename__ = "waitlist_leads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    # Lowercased by services.waitlist before insert; UNIQUE is what makes the
    # public endpoint idempotent (re-submitting updates instead of duplicating).
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    # Which purchase the blocked click was for — the catalog ids joined by ",",
    # e.g. "secretaria_basico" or "complete_clinic_combo,multi_professional".
    # Free-form on purpose: it is a sales hint, not an enum to validate against a
    # catalog that may well have changed by launch day.
    plan_hint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
