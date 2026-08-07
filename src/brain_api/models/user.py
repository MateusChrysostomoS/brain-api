"""User model — a person who logs into the Brain portal.

`password_hash` (bcrypt) is a secret-by-convention column: it is NEVER declared on any
`*Out` response schema and NEVER logged (the structlog redactor blanks `password_hash`).
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base

# Allowed role values (validated at the application layer; stored as a plain string).
# `admin` is the platform role (unchanged). `doctor`/`manager` replaced
# `tenant_owner`/`tenant_staff` (role-taxonomy round): a `manager` gets every gate a
# `doctor` gets (see `api/deps.require_doctor`) — the split is `is_owner`/`is_manager`
# below, not the role string.
ROLE_ADMIN = "admin"
ROLE_DOCTOR = "doctor"
ROLE_MANAGER = "manager"
# LEGACY (pre role-taxonomy round): no longer written by any code path, but STILL
# ACCEPTED when validating an already-issued token during the ~30min post-deploy
# transition window (api/deps.py's *_LEGACY_* tuples) — remove once every old token has
# expired.
ROLE_TENANT_OWNER = "tenant_owner"
ROLE_TENANT_STAFF = "tenant_staff"
ROLES = (ROLE_ADMIN, ROLE_DOCTOR, ROLE_MANAGER)


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
    # admin | doctor | manager (+ LEGACY tenant_owner/tenant_staff, read-only — see ROLES)
    role: Mapped[str] = mapped_column(String(32), default=ROLE_DOCTOR)
    # "Also a manager": a doctor with manager powers. Stored + exposed today; no backend
    # gate of its own yet (product: a manager gate will layer on top later).
    is_manager: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )
    # The clinic OWNER — whoever bought the clinic. Preserves the pre-taxonomy
    # owner-only gates (onboarding pause, "owner of this tenant" lookups). Exactly one
    # per tenant in the steady state (signup provisioning sets it once); not DB-enforced.
    is_owner: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), default=False)

    # Cross-service value reference to secretaria.professionals.id — deliberately NO
    # ForeignKey: the row lives in secretaria's own (separate) database, so a DB-level FK
    # is impossible (same convention as PrecheckAccountLink.precheck_user_id). Set for a
    # doctor/manager that IS (or has claimed) a clinic professional; carried into the
    # access/hub token claims (core/security.py) when present.
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
