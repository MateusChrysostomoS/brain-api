"""User model — a person who logs into the Brain portal.

`password_hash` (bcrypt) is a secret-by-convention column: it is NEVER declared on any
`*Out` response schema and NEVER logged (the structlog redactor blanks `password_hash`).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base

# Allowed role values (validated at the application layer; stored as a plain string).
ROLE_ADMIN = "admin"
ROLE_TENANT_OWNER = "tenant_owner"
ROLE_TENANT_STAFF = "tenant_staff"
ROLES = (ROLE_ADMIN, ROLE_TENANT_OWNER, ROLE_TENANT_STAFF)


class User(Base):
    """A portal user. `tenant_id` is NULL for a platform admin."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # NULL for a platform admin; otherwise the tenant this user acts for.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Stored lower-cased; unique across the platform.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    # bcrypt hash. NEVER serialized to a response, NEVER logged.
    password_hash: Mapped[str] = mapped_column(String(255))
    # admin | tenant_owner | tenant_staff
    role: Mapped[str] = mapped_column(String(32), default=ROLE_TENANT_OWNER)

    # Cross-service value reference to secretaria.professionals.id — deliberately NO
    # ForeignKey: the row lives in secretaria's own (separate) database, so a DB-level FK
    # is impossible (same convention as PrecheckAccountLink.precheck_user_id). Set for a
    # tenant_owner/tenant_staff that IS (or has claimed) a clinic professional; carried
    # into the access/hub token claims (core/security.py) when present.
    professional_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    # sha256 hex of a single-use professional-invite token (POST
    # /doctor/professionals/invites, B2) — same hashed-at-rest scheme as
    # RefreshToken.token_hash / SignupIntent.onboarding_token_hash. Burned (nulled) on
    # redemption via POST /auth/exchange-invite-token.
    invite_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    invite_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
