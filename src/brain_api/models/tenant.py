"""Tenant model — one clinic / organization on the Brain platform.

Non-sensitive identity only. Per the tenant-secrets-encryption skill, any tenant
secret would live in a separate `tenant_credentials` table (NOT created in this scope,
since brain-api stores no tenant secrets yet).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class Tenant(Base):
    """A clinic/organization. Owns users and a single entitlements row."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    clinic_name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
