"""StaffPasswordResetCode — the pending e-mailed reset code for one staff user.

At most one row per user (`user_id` is the primary key): issuing a new code OVERWRITES
the row, which invalidates the previous code and zeroes `attempts`. The code itself
exists only in the e-mail; the row keeps an HMAC-SHA256 keyed by SECRET_KEY, so a
database leak alone does not reveal live codes.

`window_started_at` / `issued_in_window` persist the per-user hourly issue cap, so a
process restart (which empties the in-memory limiters) does not reset it.

TASK-056. When the multi-clinic roadmap moves the password to a personal account,
this table moves with `users.password_hash` (z_prompts/ROTEIRO_EQUIPE_CONTA_MULTI_CLINICA.md).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class StaffPasswordResetCode(Base):
    __tablename__ = "staff_password_reset_codes"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_in_window: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
