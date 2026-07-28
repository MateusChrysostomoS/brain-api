"""Pydantic v2 schemas for the doctor onboarding + multi-professional vertical
(CONTRACT_onboarding_v1.md §7). Doctor/public-facing request+response shapes only — the
pure state-machine constants live in `services/onboarding.py`, the secretaria-facing
internal payloads (the crons' pull/push, §5) in `schemas/internal.py`.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from brain_api.services import onboarding


class LastAttemptOut(BaseModel):
    """The most recent `SignupAttempt`, embedded in `GET /doctor/onboarding`."""

    attempt_id: UUID
    result: str
    blocker_reason: str | None
    error_code: str | None
    created_at: datetime


class EmbeddedSignupOut(BaseModel):
    """Whether the Meta JS SDK Embedded Signup flow is wired up server-side.

    `configured` requires BOTH `app_id` and `config_id` to be set — the frontend uses it
    to decide whether "Tentar ativar agora" launches Embedded Signup or renders
    disabled-with-a-note (CONTRACT_onboarding_v1.md §13).
    """

    configured: bool
    app_id: str | None
    config_id: str | None


class OnboardingStateOut(BaseModel):
    """`GET /doctor/onboarding` — CONTRACT_onboarding_v1.md §7."""

    onboarding_state: str
    blocker_reason: str | None
    config_status: str
    connected: bool
    mode_resolved: bool
    secretaria_provisioned: bool
    next_retry_at: datetime | None
    retry_paused: bool
    config_reminder_paused: bool
    last_attempt: LastAttemptOut | None
    embedded_signup: EmbeddedSignupOut


class OnboardingActionOut(BaseModel):
    """Shared response shape for resolve-blocker / pause — the tenant's onboarding state
    right after the mutation, so the frontend can update its screen with no extra
    round-trip."""

    onboarding_state: str
    blocker_reason: str | None


class AttemptIn(BaseModel):
    """`POST /doctor/onboarding/attempts` body — CONTRACT_onboarding_v1.md §7.

    `attempt_id` is the idempotency key (`services.onboarding.record_attempt`).
    `phone_number_id` is required when `result=='pass'` (422 otherwise); `code` is
    optional even on a pass (the Meta token exchange is skipped when absent).
    """

    model_config = ConfigDict(extra="forbid")

    attempt_id: UUID
    result: Literal["pass", "fail"]
    code: str | None = None
    phone_number_id: str | None = None
    waba_id: str | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def _phone_number_id_required_for_pass(self) -> "AttemptIn":
        if self.result == onboarding.ATTEMPT_RESULT_PASS and not self.phone_number_id:
            raise ValueError("phone_number_id is required when result=='pass'")
        return self


class AttemptOut(BaseModel):
    """`POST /doctor/onboarding/attempts` response. `replayed=True` means `attempt_id`
    was already recorded — the tenant's state was NOT transitioned again."""

    attempt_id: UUID
    replayed: bool
    onboarding_state: str
    blocker_reason: str | None


class PauseIn(BaseModel):
    """`POST /doctor/onboarding/pause` body (owner only) — the kill switches. Each field
    sets the matching `tenants.retry_paused`/`config_reminder_paused` column directly
    when present; omitted/`null` leaves that switch untouched."""

    model_config = ConfigDict(extra="forbid")

    retries: bool | None = None
    config_reminders: bool | None = None


class ProfessionalOut(BaseModel):
    """One row of `GET /doctor/professionals` — secretaria's config-status professional
    entry (CONTRACT_onboarding_v1.md §4 item 3) plus the LOCAL brain-api user linkage."""

    id: str
    name: str
    is_active: bool = True
    has_calendar: bool = False
    has_hours: bool = False
    has_services: bool = False
    complete: bool = False
    linked_user_email: str | None = None
    invite_pending: bool = False


class ProfessionalsOut(BaseModel):
    items: list[ProfessionalOut]


class ProfessionalInviteIn(BaseModel):
    """`POST /doctor/professionals/invites` body. Open to any doctor (owner or staff) —
    corrections round, 2026-07-22; previously owner-only."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    email: EmailStr = Field(max_length=320)
    specialty: str | None = Field(default=None, max_length=255)


class ProfessionalInviteOut(BaseModel):
    """`invite_link` is ALWAYS present (even when the notification email failed to
    send — fail-soft, CONTRACT_onboarding_v1.md scope C) so the owner can share it
    manually."""

    professional_id: UUID
    user_id: UUID
    invite_link: str


class ProfessionalSelfIn(BaseModel):
    """`POST /doctor/professionals/self` body. Open to any doctor (owner or staff) —
    corrections round, 2026-07-22; previously owner-only. `name` defaults to the
    caller's own user name (then the clinic name) when omitted."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=255)
    specialty: str | None = Field(default=None, max_length=255)


class ProfessionalSelfOut(BaseModel):
    professional_id: UUID
    created: bool


class TestWindowOut(BaseModel):
    """`GET /doctor/onboarding/test-window` — Task 2 (Meta/WABA acceptance test-window
    reframe). `applicable` mirrors the plan/day/subscription gates
    `services.onboarding_sync.test_window_email_due` uses for the internal cron, minus the
    deadline/notified checks — this is a live status read, not a one-shot flag.
    `subscription_status` is the raw `Entitlement.status` (None with no entitlement row).
    """

    applicable: bool
    days_total: int
    started_at: datetime | None
    deadline_at: datetime | None
    onboarding_state: str
    connected_at: datetime | None
    expired: bool
    notified: bool
    subscription_status: str | None
    can_restart: bool


class TestWindowRestartOut(BaseModel):
    """`POST /doctor/onboarding/test-window/restart` response."""

    restarted: bool
    deadline_at: datetime
    payment_method_present: bool
