"""Account-link model — maps a brain user to their PreCheck user (SSO bridge).

The two services have independent identity:
- brain users have a **UUID** primary key (`users.id`, this database);
- PreCheck users have an **integer** primary key (`users.id`, PreCheck's OWN database).

A row here is the explicit, operator-established claim "this brain person IS that
PreCheck doctor", created during onboarding for a clinic that owns both products. It is
the ONLY thing that lets brain-api mint a PreCheck session for a brain login
(see services/sso.py). Holding it in brain's DB keeps PreCheck untouched.

`precheck_user_id` is a logical reference to `precheck.users.id` in a SEPARATE database,
so it carries NO database-level foreign key (cross-database FKs are impossible). Integrity
of that value is an onboarding responsibility (the link utility points it at a real id).

Never serialized to a public response and never logged (it is identity-mapping data).
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class PrecheckAccountLink(Base):
    """One-to-one link: brain user (UUID) -> PreCheck user (int), scoped to a tenant."""

    __tablename__ = "precheck_account_links"
    __table_args__ = (
        # A brain user maps to at most one PreCheck user (mandated). The reverse unique on
        # precheck_user_id is defensive: it stops two brain users from claiming the SAME
        # PreCheck identity, which would silently cross-wire two logins to one doctor.
        UniqueConstraint("brain_user_id", name="uq_precheck_links_brain_user"),
        UniqueConstraint("precheck_user_id", name="uq_precheck_links_precheck_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # The brain user this link belongs to. CASCADE: drop the link if the brain user is
    # deleted (the link is meaningless without them). The uq_ constraint below already
    # provides the lookup index (hot path: select by brain_user_id), so no extra index.
    brain_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # Logical reference to precheck.users.id (a DIFFERENT database) — no FK by design.
    # BigInteger to be safe against PreCheck's id growth; values are small today.
    precheck_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # The tenant context the link is valid for (defense-in-depth: the SSO service asserts
    # this matches the acting principal's tenant before minting).
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
