"""Pydantic v2 schemas for brain-api's INBOUND /internal/* surface (secretarIA-facing).

Service-to-service payloads only (X-Internal-Api-Key callers — today secretarIA).
Nothing here carries a secret, a password hash, or a raw token back out: the hub token
arrives in the request body, is validated in-memory, and only booleans/ids leave.
"""

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain_api.services import catalog

_PHONE_RE = re.compile(r"^\d{8,15}$")


class HubTokenVerifyIn(BaseModel):
    """`POST /internal/secretaria/hub-token/verify` body — the opaque hub bearer."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=2048)


class HubTokenVerifyOut(BaseModel):
    """Introspection result: is this hub session allowed to act, and for which tenant.

    `active` is the LIVE answer (valid token AND entitlement active/trialing AND
    secretaria enabled) — secretarIA never caches it beyond a short TTL and never
    decides it locally. `tenant_id` is present whenever the token itself was valid,
    so a refused-but-valid session can be logged tenant-scoped on the caller side.
    `professional_id` rides the same way (present when the token carried one, parsed
    safely — a malformed claim reads as absent, never a 500).
    """

    active: bool
    tenant_id: UUID | None = None
    professional_id: UUID | None = None


class InternalEntitlementOut(BaseModel):
    """`GET /internal/tenants/{tenant_id}/entitlements` — the summary secretarIA's
    plugin gates consume (same `is_entitled` semantics, evaluated from these fields)."""

    tenant_id: UUID
    status: str
    active: bool
    secretaria_enabled: bool
    plan: str
    secretaria_tier: str | None
    addons: dict = Field(default_factory=dict)
    limits: dict = Field(default_factory=dict)


class UsageEventIn(BaseModel):
    """`POST /internal/usage-events` body — one billable/meterable action, already done.

    `event_id` is the CALLER's own idempotency key (e.g. "reminder:24h:<appointment_id>")
    — events get redelivered, and `services/usage.py` dedupes on it via `session.get`
    before applying anything. `feature` must be a catalog limit key (`LIMIT_KEYS`) so a
    typo'd feature name 422s instead of silently creating an untracked counter.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    feature: str
    amount: int = Field(ge=1, le=10000)
    event_id: str = Field(min_length=1, max_length=128)

    @field_validator("feature")
    @classmethod
    def _feature_in_catalog(cls, v: str) -> str:
        if v not in catalog.LIMIT_KEYS:
            raise ValueError(f"unknown feature {v!r}; known: {sorted(catalog.LIMIT_KEYS)}")
        return v


class UsageEventOut(BaseModel):
    """`POST /internal/usage-events` response. `recorded=False` means a duplicate
    `event_id` was replayed — the ledger and the entitlement counter were NOT touched
    again. Always `200` either way (a duplicate is not an error, per idempotency)."""

    recorded: bool


class PrecheckHandoffIn(BaseModel):
    """`POST /internal/precheck-handoff` body — secretarIA identifies a patient by
    tenant + WhatsApp phone; brain-api resolves entitlement and forwards to PreCheck
    (CONTRACTS.md §12.3). `phone_number` is digits only (no `+`/spaces/punctuation),
    8-15 chars — the same shape PreCheck's own contract expects.

    `patient_name`/`booked_service` are OPTIONAL booking context (FEAT 38) forwarded
    verbatim to PreCheck; they do not participate in any gate here."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    phone_number: str = Field(min_length=8, max_length=15)

    # --- Optional booking context (FEAT 38) --------------------------------------
    # This model is the STRICT hop of the mesh (`extra="forbid"` above): until it knows
    # a field name, a sender using it 422s the WHOLE handoff — including the already
    # -shipped trigger that needs neither field. So this widening must be live BEFORE
    # secretarIA starts sending (FEAT 39); see the `frozen-contract-migration` skill.
    # `extra="forbid"` stays exactly as it was — this enlarges the KNOWN field set, it
    # does not relax validation.
    #
    # Both default to None so this schema deployed ALONE is a no-op: today's two-field
    # body keeps validating and forwarding byte-identically.
    #
    # PII: `patient_name` is patient-identifying free text — it must never reach a log
    # line on this path, nor an exception rendered into one (services/precheck_handoff.py).
    patient_name: str | None = Field(default=None, max_length=255)
    # The clinic's own appointment type, RAW — no normalization (product decision).
    booked_service: str | None = Field(default=None, max_length=255)

    @field_validator("phone_number")
    @classmethod
    def _phone_shape(cls, v: str) -> str:
        if not _PHONE_RE.match(v):
            raise ValueError("phone_number must be 8-15 digits")
        return v


class PrecheckHandoffOut(BaseModel):
    """`POST /internal/precheck-handoff` response — PreCheck's own 200 body passed
    through verbatim (never re-derived): `seeded` for a freshly pre-seeded session,
    `already_active` when the patient already had one live."""

    status: Literal["seeded", "already_active"]


# --- Onboarding crons (CONTRACT_onboarding_v1.md §5 items 7-8; secretaria pulls/posts) ---


class InternalOnboardingTenantOut(BaseModel):
    """One row of `GET /internal/onboarding/tenants` — everything secretaria's onboarding
    crons (`run_onboarding_nudges`) need to decide who gets a retry nudge / config
    reminder / D+30 flag / D+60 closing email, without a second round-trip."""

    tenant_id: UUID
    onboarding_state: str
    blocker_reason: str | None
    config_status: str
    onboarding_anchor_at: datetime | None
    next_retry_at: datetime | None
    retry_paused: bool
    config_reminder_paused: bool
    config_reminder_anchor_at: datetime | None
    last_config_reminder_at: datetime | None
    closing_email_sent_at: datetime | None
    manual_review_flagged_at: datetime | None
    owner_email: str | None
    owner_name: str | None
    clinic_name: str
    subscription_active: bool
    # --- Task 2: Meta/WABA acceptance test-window reframe -----------------------------
    #: Whether this tenant's paid Stripe trial ran out before it ever reached
    #: 'conectado'/'ativo' and nobody has been notified yet
    #: (services.onboarding_sync.test_window_email_due). secretaria's cron POSTs
    #: `test_window_email_sent` (below) once it has actually sent the email.
    test_window_email_due: bool
    #: `Settings.STRIPE_TRIAL_PERIOD_DAYS` — the test-window length in days, so the cron
    #: never has to hardcode it.
    test_window_days: int
    #: `{FRONTEND_BASE_URL}/app/reativar` — where the email should send the tenant to
    #: restart the window (POST /doctor/onboarding/test-window/restart).
    test_window_restart_url: str


class InternalOnboardingListOut(BaseModel):
    items: list[InternalOnboardingTenantOut]


class InternalOnboardingEventIn(BaseModel):
    """`POST /internal/onboarding/tenants/{tenant_id}/events` body — one cron
    bookkeeping event. `retry_nudge_sent`/`config_reminder_sent` are RECURRING and
    always applied; `closing_email_sent`/`manual_review_flagged`/`test_window_email_sent`
    are ONE-SHOT (idempotent no-op once already set) — see
    `services/onboarding_sync.py::apply_onboarding_event`. `test_window_email_sent`
    (Task 2) sets `tenants.test_window_notified_at = at`.
    """

    model_config = ConfigDict(extra="forbid")

    event: Literal[
        "retry_nudge_sent",
        "config_reminder_sent",
        "closing_email_sent",
        "manual_review_flagged",
        "test_window_email_sent",
    ]
    at: datetime
    next_retry_at: datetime | None = None


class InternalOnboardingEventOut(BaseModel):
    """`applied=False` means a one-shot marker was already set — not an error."""

    applied: bool


class InternalProfessionalEmailOut(BaseModel):
    """One professional's contact address, as secretarIA needs to reach them.

    `professional_id` is the SECRETARIA-side id (`users.professional_id`),
    which is what the caller holds — it knows appointments, not brain-api user
    ids. Serialised as a string so the consumer can compare it against
    `str(appointment.professional_id)` without a parse step.
    """

    professional_id: str
    email: str


class InternalProfessionalEmailsOut(BaseModel):
    """`GET /internal/tenants/{tenant_id}/professional-emails` response.

    A batch over the WHOLE tenant rather than a per-professional lookup: the
    consumer (secretarIA's post-booking notification hook) asks once per
    booking, and a per-id endpoint would invite an N+1 the moment anything
    needs two. Professionals with no linked user are simply absent — this is a
    "who can we reach" answer, not a roster.
    """

    items: list[InternalProfessionalEmailOut]
