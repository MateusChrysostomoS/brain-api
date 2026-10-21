"""Personal staff credentials; clinical authority remains on User, never here."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class StaffAccount(Base):
    __tablename__ = "staff_accounts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    credential_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requires_password_change: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    temporary_password_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reset_token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    reset_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    preferred_user_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StaffAccountSession(Base):
    __tablename__ = "staff_account_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_accounts.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    credential_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StaffAccountResetCode(Base):
    __tablename__ = "staff_account_reset_codes"

    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    issued_in_window: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StaffMembershipInvite(Base):
    __tablename__ = "staff_membership_invites"
    __table_args__ = (UniqueConstraint("account_id", "tenant_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    membership_version: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_accounts.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32))
    is_manager: Mapped[bool] = mapped_column(Boolean, default=False)
    professional_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_status: Mapped[str] = mapped_column(String(32), default="not_requested")
