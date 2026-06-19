"""DemoRequest model — isolated lead capture for the "Agendar demo" form.

Does NOT reference tenants/users/entitlements. A demo request never creates a tenant
and never touches billing — it is a sales lead, full stop. `profile` / `product_interest`
are validated against enums at the schema layer and stored as plain strings here.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class DemoRequest(Base):
    """A captured demo/lead request from the public marketing forms."""

    __tablename__ = "demo_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(320), index=True)
    clinic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # clinica_privada | medico_autonomo | secretaria_municipal | hospital | outro
    profile: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # precheck | secretaria | ambos
    product_interest: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which surface the lead came from: brain | secretaria | precheck
    source: Mapped[str | None] = mapped_column(String(32), nullable=True, default="brain")
    # Lead pipeline status: new | contacted | converted | dismissed
    status: Mapped[str] = mapped_column(String(32), server_default="new", default="new")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
