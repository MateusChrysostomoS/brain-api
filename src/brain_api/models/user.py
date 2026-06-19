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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
