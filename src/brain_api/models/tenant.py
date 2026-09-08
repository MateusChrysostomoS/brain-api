"""Tenant model — one clinic / organization on the Brain platform.

Non-sensitive identity only. Per the tenant-secrets-encryption skill, any tenant
secret would live in a separate `tenant_credentials` table (NOT created in this scope,
since brain-api stores no tenant secrets yet).

The onboarding state machine (`onboarding_state` / `blocker_reason` / `config_status` +
the timestamps and pause flags below) is OWNED by `services/onboarding.py` — the single
writer for every transition (CONTRACT_onboarding_v1.md §8). Every default here is one of
the enum-value constants declared there; nothing on this model is a magic string.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from brain_api.core.database import Base
from brain_api.services import onboarding


class Tenant(Base):
    """A clinic/organization. Owns users and a single entitlements row."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    clinic_name: Mapped[str] = mapped_column(String(255))

    # --- Onboarding state machine (services/onboarding.py is the sole writer) ---------
    onboarding_state: Mapped[str] = mapped_column(
        String(40),
        server_default=onboarding.STATE_PENDING,
        default=onboarding.STATE_PENDING,
    )
    blocker_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    config_status: Mapped[str] = mapped_column(
        String(40),
        server_default=onboarding.CONFIG_STATUS_INCOMPLETA,
        default=onboarding.CONFIG_STATUS_INCOMPLETA,
    )
    onboarding_anchor_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    secretaria_provisioned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Espelho exato do irmão acima, para o bridge do PreCheck (0015): NULL = ainda
    # não provisionado (retentar), carimbado = no-op.
    precheck_provisioned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    config_reminder_anchor_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_config_reminder_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closing_email_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    manual_review_flagged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Owner-only kill switches (POST /doctor/onboarding/pause, B2) for the secretarIA
    # retry-nudge / config-reminder crons.
    retry_paused: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), default=False)
    config_reminder_paused: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )

    # --- Meta/WABA acceptance test-window (Task 2; migration 0009_test_window) ----------
    # The Stripe trial is reframed as a test window to accept WhatsApp Coexistence, not a
    # generic "try before you buy" period. `test_window_started_at` anchors it — set at
    # payment completion (services/signup.py::provision_tenant_from_intent), reset on any
    # genuine subscription-id change (services/billing.py's
    # `_reset_markers_if_subscription_changed`), and on a manual restart
    # (POST /doctor/onboarding/test-window/restart). `test_window_notified_at` is the
    # one-shot "the D+N past-deadline email was sent" marker
    # (services/onboarding_sync.py::apply_onboarding_event, event="test_window_email_sent").
    test_window_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    test_window_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Delivery channels (migration 0017_message_channels) ---------------------------
    # HOW this clinic talks to its patients, not WHAT it bought: a channel is an
    # operational property of the TENANT, never of the plan (`services/catalog.py`'s
    # `PlanDef` is deliberately untouched — a clinic switches channel without switching
    # plan).
    #
    # Two booleans, not one enum: they mirror `Entitlement.precheck_enabled` /
    # `secretaria_enabled`, which already model "which ones" as a SET rather than an
    # exclusive choice. A clinic plausibly migrates gradually (keeps WhatsApp for its
    # existing patients, offers Brain-Message to the new ones); a single enum would force
    # an all-or-nothing cutover that does not match the real case.
    #
    # 0017 backfilled `whatsapp_enabled` for every tenant with a stamped `connected_at` —
    # the only signal available in this repo that a WABA was actually connected
    # (`services/onboarding.py::record_attempt` stamps it on a 'pass' attempt). No new
    # tenant is born with a channel on; whoever provisions the channel turns it on.
    whatsapp_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )
    brain_message_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
