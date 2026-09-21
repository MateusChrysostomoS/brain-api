"""Pydantic v2 schemas for brain-api's INBOUND /internal/* surface (secretarIA-facing).

Service-to-service payloads only (X-Internal-Api-Key callers — today secretarIA).
Nothing here carries a secret, a password hash, or a raw token back out: the hub token
arrives in the request body, is validated in-memory, and only booleans/ids leave.
"""

import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

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
    """`POST /internal/precheck-handoff` body — secretarIA names the patient by tenant + ONE
    handle; brain-api resolves entitlement and forwards to PreCheck (CONTRACTS.md §12.3).

    `phone_number` is digits only (no `+`/spaces/punctuation), 8-15 chars — the same shape
    PreCheck's own contract expects.

    `external_id` (2026-09-19, TASK-003 §5.1) is the alternative for a patient of the **Portal**,
    who has `wa_id IS NULL` by design and therefore no phone number to be named by. It is
    brain-api's `MessagePatient.id`, spelled as the caller knows it — the same name and the same
    value `PendingEmailClaimIn`/`PendingIdentityIn` already use, and the same handle
    `message_switchboard.send_message` sends as `external_id` on every relayed message.

    **EXACTLY ONE of the two, never both and never neither** — both present is `422`, as is
    neither. The pair is not a preference order: a body carrying both would be describing two
    different patients and there is no defensible rule for picking one.

    Declared as `UUID` rather than the contract's literal `str | None`: the handle IS a UUID
    here (`MessagePatient.id`) and both sibling internal schemas already type it that way, so a
    value that cannot be one could never resolve to a patient. Registered as a deviation in
    `tasks/TASK-003/results/brain-api.md`.

    > **The `external_id` leg reaches PreCheck through a DIFFERENT route** —
    > `/internal/brain-message/open`, not `/internal/precheck-handoff`, which is keyed on a
    > phone number. See CONTRACTS.md §12.3.2 (TASK-004) for why the inbound route was not it.

    `patient_name`/`booked_service` are OPTIONAL booking context (FEAT 38) forwarded
    verbatim to PreCheck; they do not participate in any gate here. `booked_service` rides
    only the `phone_number` leg — PreCheck's opening route does not declare it."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    # Optional ONLY in the schema sense: the model validator below still demands one of the
    # two handles, so the legacy two-field body validates and 422s exactly as it did.
    phone_number: Annotated[str, StringConstraints(min_length=8, max_length=15)] | None = None
    external_id: UUID | None = None

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
    def _phone_shape(cls, v: str | None) -> str | None:
        # `None` reaches here only when the caller sent an explicit `"phone_number": null`;
        # an omitted field keeps the default without running validators. Either way the
        # absence is the model validator's business, not the shape check's.
        if v is not None and not _PHONE_RE.match(v):
            raise ValueError("phone_number must be 8-15 digits")
        return v

    @model_validator(mode="after")
    def _exactly_one_handle(self) -> "PrecheckHandoffIn":
        """One handle, never two, never none — `422` otherwise (TASK-003 §5.1).

        Written as an equality on two `is None` tests so both wrong shapes take the SAME
        branch and produce the same message: a caller must not be able to learn, from the
        error, which of the two this service would have preferred.
        """
        if (self.phone_number is None) == (self.external_id is None):
            raise ValueError("exactly one of phone_number or external_id is required")
        return self


class PrecheckHandoffOut(BaseModel):
    """`POST /internal/precheck-handoff` response — PreCheck's own 200 body passed
    through verbatim (never re-derived): `seeded` for a freshly pre-seeded session,
    `already_active` when the patient already had one live."""

    status: Literal["seeded", "already_active"]


# --- The pending visit's e-mail (2026-09-16, chat-first onboarding) ----------------------


class PendingEmailClaimIn(BaseModel):
    """`POST /internal/brain-message/pending-email` — secretarIA captured an address in chat.

    THIS HOP IS WHY THE PATIENT'S OWN CODE IS SAFE. In the owner's flow the visitor types their
    e-mail into the conversation, books a real appointment, and only then receives a code. The
    address must therefore travel from the conversation to brain-api WITHOUT passing through the
    browser: if a client could name the address it wanted verified, holding a pending token would
    be enough to aim a code at anybody's inbox. So it arrives on the SERVICE leg
    (`X-Internal-Api-Key`, the same pair key every other `/internal/*` route uses) and the
    patient-facing `POST /patient-access/pending/request-otp` takes no address at all.

    `external_id` is what secretarIA already holds for this conversation — brain-api's
    `MessagePatient.id`, sent to it as `external_id` on every relayed message
    (`services/message_switchboard.py::send_message`). Named here as the CALLER knows it, not as
    this repo spells it internally (`patient_ref`), so nothing has to be renamed on the wire.

    CLAIMED, NOT PROVEN: this records an address against a conversation and grants nothing. Only
    a verified code moves it onto an identity or into an account.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    external_id: UUID
    # `EmailStr` so a typo the conversation captured fails here, loudly, instead of silently
    # becoming an address no code can ever reach.
    email: EmailStr


class PendingEmailClaimOut(BaseModel):
    """`POST /internal/brain-message/pending-email` response.

    `claimed` is the only success. A conversation that is not a live pending visit — unknown
    handle, wrong clinic, expired, already verified — is a 404 with ONE detail for every
    reason: secretarIA must not be able to tell them apart, and there is nothing it could do
    differently if it could.

    `account_exists`/`email_masked` (2026-09-21) say whether the address just claimed is
    already an account's, so secretarIA can ask the right next question: a name for somebody
    new, the six-digit code for somebody returning. `email_masked` is `core/email_mask.py`'s
    form of that SAME address — first character, `***`, last character, whole domain — and
    is present only alongside `account_exists=True`; the raw value never crosses this
    boundary, here or anywhere else in this file.

    ADDITIVE AND SAFE IN EITHER DEPLOY ORDER, like `PendingOtpRequestOut.email_masked` below:
    both fields carry a default, and today's consumer reads the STATUS CODE only and never
    parses this body (`secretarIA/services/pending_identity.py::claim_email`). A secretarIA
    older than this service therefore keeps working unchanged, and a newer one reads the
    fields best-effort — their absence is an older brain-api, not an error.

    WHY `account_exists` IS NOT AN ENUMERATION LEAK WORTH REFUSING (owner's decision,
    2026-09-20): this is the SERVICE leg, reachable only with `X-Internal-Api-Key`, and the
    one bit it adds is about an address the visitor typed themselves. The mask describes that
    same typed address, so it tells its owner nothing they did not just write. That the
    product then says "your e-mail is already with us" in the chat is the owner's explicit
    decision, recorded in `docs/CHECKPOINT_portal_email_ja_cadastrado.md`.
    """

    status: Literal["claimed"]
    account_exists: bool = False
    email_masked: str | None = None


class PendingIdentityIn(BaseModel):
    """Common key for secretarIA's pending-identity service leg.

    Both fields must match the same visit. `external_id` is the caller's name for
    `MessagePatient.id`; no e-mail or token is accepted on this boundary.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    external_id: UUID


class PendingIdentityStatusOut(BaseModel):
    """PII-free state used before secretarIA decides whether to ask for e-mail."""

    status: Literal[
        "pending_unclaimed",
        "pending_claimed",
        "verified",
        "unknown",
    ]


class PendingOtpRequestOut(BaseModel):
    """The code was accepted for delivery; neither address nor credential leaves.

    `email_masked` (2026-09-19, TASK-003 §3) is the ONE exception, and it is not the address:
    `core/email_mask.py` gives back first character + `***` + last character of the local part
    plus the whole domain, so the notice secretarIA renders can say "digite o código enviado
    para a***a@gmail.com" without the raw value ever crossing this boundary. secretarIA holds
    no copy of the address — that is the whole point of the claim living on the service leg
    (`PendingEmailClaimIn`) — so it cannot compute this itself, and it must not be given the
    address just so it can.

    ADDITIVE AND SAFE IN EITHER DEPLOY ORDER. A caller that ignores the field keeps working;
    a caller that wants it gets it the moment this service is live. It is declared non-optional
    because on a `200` there is always an address to describe — a visit with none is the `409
    pending_email_missing` above, never a `200` with a null.
    """

    status: Literal["sent"]
    email_masked: str


class PendingOtpVerifyIn(PendingIdentityIn):
    """Inline code from secretarIA. The code is consumed in-memory and never logged."""

    code: str = Field(min_length=1, max_length=32)


class PendingOtpVerifyOut(BaseModel):
    """The identity is proven; the browser still has to call `/pending/complete`."""

    status: Literal["verified"]


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
