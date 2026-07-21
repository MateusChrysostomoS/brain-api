"""SignupAttempt model — idempotency ledger for onboarding WhatsApp-connection attempts.

One row per (client-supplied) attempt id: the connection UI (embedded signup or a
guided retry, B2's `POST /doctor/onboarding/attempts`) posts an attempt with a
caller-chosen `attempt_id`, and the PK IS the idempotency key (mirrors `UsageEvent`'s
natural-key dedupe pattern) — a replayed POST with the same id is a no-op, never a
duplicate row or a double state transition.

The single writer for both this table's rows and the `tenants` state-machine columns
they drive is `services/onboarding.py::record_attempt` (CONTRACT_onboarding_v1.md §8) —
no other code path inserts here.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class SignupAttempt(Base):
    """One recorded WhatsApp-connection attempt (pass/fail) for a tenant's onboarding."""

    __tablename__ = "signup_attempts"

    # Client-supplied attempt id — the PK IS the idempotency key (no server-generated id).
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # user | retry_nudge | intake (services.onboarding.ATTEMPT_SOURCES) — what triggered it.
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    day_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # pass | fail (services.onboarding.ATTEMPT_RESULTS)
    result: Mapped[str] = mapped_column(String(10), nullable=False)
    # The RESOLVED blocker (services.onboarding.map_error_to_blocker), not the raw error.
    blocker_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
