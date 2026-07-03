"""PrivacyRequest model — the LGPD audit trail for cross-database erasure/export.

Three services (`brain`, `secretaria`, `precheck`) hold SEPARATE databases with no
cross-DB FK/cascade by design (docs/cross-db-erasure.md); brain-api orchestrates a
fan-out erasure/export and records exactly one row per invocation here.

This is deliberately an audit trail that SURVIVES the erasure it records: a row never
carries the raw subject identifier (email/wa_id), only a sha256 hash of the normalized
subject key (`services/privacy.py::subject_hash`), and `result` carries COUNTS + a
per-service status string ONLY — never personal data (never the exported bundle).
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base


class PrivacyRequest(Base):
    """One LGPD erasure/export invocation (`POST /admin/privacy/erasure|export`)."""

    __tablename__ = "privacy_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # erasure | export
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # patient | user
    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # sha256 hex of the normalized subject key — NEVER the raw email/wa_id.
    subject_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # The acting admin. SET NULL (not CASCADE): the audit row must outlive the admin
    # account that requested it.
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # completed | partial
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Counts + per-service status ONLY (services/privacy.py builds this). Never PII.
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
