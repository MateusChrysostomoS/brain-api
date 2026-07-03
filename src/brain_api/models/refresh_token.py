"""Refresh-token model — the revocable long-lived leg of a session (auth-jwt-multitenant).

The access JWT stays short and unrevocable; THIS row is what a logout, a rotation or a
plan cancellation can kill. Only a SHA-256 hash of the opaque token is stored (a DB read
never yields a usable credential); the raw value exists client-side only. Rotated on
every use: the presented row is revoked and a fresh one issued. A presented token that
is already revoked is a reuse/theft signal — the service revokes the user's whole
active family.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class RefreshToken(Base):
    """One issued refresh token (hashed). Revoked rows are kept for reuse detection."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # SHA-256 hex of the opaque token value — never the value itself.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
