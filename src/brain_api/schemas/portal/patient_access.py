"""Schemas for the patient side of the Brain-Message channel (account model, 2026-09-15).

Two kinds of field live here:

- the ACCOUNT contract: e-mail + code open an account; a clinic enters it by invite (its
  link, its short code `tenants.patient_invite_code`, or its UUID);
- TRANSITION fields, marked DEPRECATED, that the portal deployed on 2026-09-14 still sends or
  reads (`tenant_id` on the login bodies; `access_token`/`tenant_id`/`patient_ref`/
  `clinic_name`/`sibling_candidates`/`linked_sessions` on the answers). The backend deploys
  first, so they must keep working until the portal that no longer uses them is live
  (docs/CHECKPOINT_portal_clinicas_convite.md, frozen-contract-migration).
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field, model_validator

from brain_api.core.invite_codes import MAX_INVITE_LENGTH

# OpenAPI marker for the transition fields. Schema only: Pydantic's own `deprecated=` would
# warn on every read the transition code itself has to do.
_DEPRECATED = {"deprecated": True}


class OtpRequestIn(BaseModel):
    """`POST /patient-access/request-otp`."""

    model_config = ConfigDict(extra="forbid")

    # `EmailStr` (the repo already depends on email-validator) so a malformed address is
    # a 422 here rather than a delivery failure three hops later.
    email: EmailStr
    # DEPRECATED (transition): the pre-account login body. When present the code is sent
    # only while that clinic has the channel on — exactly the old behaviour.
    tenant_id: UUID | None = Field(default=None, json_schema_extra=_DEPRECATED)


class OtpVerifyIn(BaseModel):
    """`POST /patient-access/verify-otp`."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    # Bounded so a multi-megabyte "code" cannot be posted at the hashing path; the real
    # check is the constant-time comparison.
    code: str = Field(min_length=1, max_length=32)
    # The clinic's link, short code or UUID, as the patient arrived with it: the account
    # comes back with that clinic in it. An invite that names nothing usable does not fail a
    # correct code — `invited_tenant_id` just comes back null.
    invite: str | None = Field(default=None, min_length=1, max_length=MAX_INVITE_LENGTH)
    # DEPRECATED (transition): the pre-account body, read as login + invite of that clinic.
    # As before, a clinic with the channel off fails the verification (400).
    tenant_id: UUID | None = Field(default=None, json_schema_extra=_DEPRECATED)

    @model_validator(mode="after")
    def _one_way_to_name_a_clinic(self) -> "OtpVerifyIn":
        if self.invite is not None and self.tenant_id is not None:
            raise ValueError("send invite or tenant_id, not both")
        return self


class ClinicInviteIn(BaseModel):
    """`POST /patient-access/clinics` — the clinic's link, short code or UUID, as pasted."""

    model_config = ConfigDict(extra="forbid")

    invite: str = Field(min_length=1, max_length=MAX_INVITE_LENGTH)


class ClinicSessionOut(BaseModel):
    """One clinic of the account with its own access token: scope `patient_message`, ONE
    tenant, held in memory. Every thread route takes this token, never the account's."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    tenant_id: UUID
    clinic_name: str
    # `MessagePatient.id` — the same handle secretarIA and PreCheck know this patient by.
    patient_ref: UUID


class SiblingCandidateOut(BaseModel):
    """DEPRECATED (transition): another clinic ALREADY IN the account.

    For the portal deployed on 2026-09-14, which reopens every candidate marked
    `already_linked` through `POST /siblings/{tenant_id}/confirm`. It never lists a clinic
    the account does not have — there is no discovery by e-mail any more — so
    `already_linked` is always true.
    """

    tenant_id: UUID
    clinic_name: str
    already_linked: bool = True


class PatientAccountOut(BaseModel):
    """What `verify-otp` and `refresh` return: the account leg and every clinic of the account.

    The revocable leg is NOT in this body — it is the `__Host-patient_session` HttpOnly
    cookie, which is the entire point of the split (see `core/cookies.py`).
    """

    # Scope `patient_account`: adds clinics (`POST /clinics`) and ends the account; opens no
    # thread.
    account_token: str
    token_type: str = "bearer"
    expires_in: int
    # Every clinic of the account whose Brain-Message channel is on, ordered by name.
    clinics: list[ClinicSessionOut] = Field(default_factory=list)
    # `verify-otp` only: the clinic the login's invite resolved to (null without an invite, or
    # when it named nothing usable — the same answer for every reason).
    invited_tenant_id: UUID | None = None

    # --- DEPRECATED (transition), for the portal deployed on 2026-09-14 -------------------
    # The clinic this login is pinned to (`services/patient_access.py::login_clinic`): the one
    # it came in through, else its first — stable across renewals. Null while the account has
    # no clinic.
    access_token: str | None = Field(default=None, json_schema_extra=_DEPRECATED)
    tenant_id: UUID | None = Field(default=None, json_schema_extra=_DEPRECATED)
    patient_ref: UUID | None = Field(default=None, json_schema_extra=_DEPRECATED)
    clinic_name: str | None = Field(default=None, json_schema_extra=_DEPRECATED)
    # The account's OTHER clinics, each `already_linked`.
    sibling_candidates: list[SiblingCandidateOut] = Field(
        default_factory=list, json_schema_extra=_DEPRECATED
    )
    # `refresh`: every clinic of the account (the same list as `clinics`).
    linked_sessions: list[ClinicSessionOut] = Field(
        default_factory=list, json_schema_extra=_DEPRECATED
    )


class ConfirmSiblingIn(BaseModel):
    """DEPRECATED (transition): `POST /patient-access/siblings/{tenant_id}/confirm`.

    Names the clinic AGAIN, and the router refuses a body that disagrees with the path, so a
    client bug that asks for the wrong clinic fails loudly.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID


class ThreadOut(BaseModel):
    """One product tab the patient may open."""

    product: str
    clinic_name: str


class ThreadListOut(BaseModel):
    """`GET /patient-access/threads`. Empty when the channel is off — never a 404."""

    data: list[ThreadOut]


class PatientMessageIn(BaseModel):
    """`POST /patient-access/threads/{product}/messages`.

    Carries NO tenant and NO patient handle: both come from the validated session. A body
    that could name them would be the one place a patient could aim at another clinic.
    """

    model_config = ConfigDict(extra="forbid")

    # 4000 is PreCheck's own ceiling for `text`; matching it means an over-long message is
    # rejected here with a clear 422 instead of upstream with an opaque one.
    text: str = Field(min_length=1, max_length=4000)
    patient_name: str | None = Field(default=None, max_length=200)
    # The id of the reply button / list row the patient tapped in the portal, when `text`
    # is that control's title. Relayed to secretarIA ONLY (PreCheck's inbound model is
    # `extra="forbid"` and has no such field - services/message_switchboard.py drops it
    # on that leg). Never trusted here or there: secretarIA checks it against the options
    # its own recent cards offered on that conversation and routes an unknown id as
    # plain text. 256 is the longest id a card can carry (a reply button's cap; list rows
    # cap at 200), the same bound secretarIA's schema enforces.
    interactive_reply_id: str | None = Field(default=None, min_length=1, max_length=256)


class PatientAttachmentForm(BaseModel):
    """The text fields of `POST /patient-access/threads/{product}/messages` sent WITH a file.

    The `multipart/form-data` twin of `PatientMessageIn`: the same fields under the same names
    and bounds, as form fields beside ONE file part named `file` — which is not declared here
    because it is judged by its content (`core/attachments.py`), never by a model. The one
    difference is `text`: optional, because a file with no caption is a whole message, as on
    WhatsApp. An empty form field counts as absent. `extra="forbid"` for the same reason as the
    JSON body: no field may name a tenant or a patient.
    """

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, max_length=4000)
    patient_name: str | None = Field(default=None, max_length=200)
    interactive_reply_id: str | None = Field(default=None, min_length=1, max_length=256)


class PatientReadMarkIn(BaseModel):
    """`POST /patient-access/threads/{product}/messages/read` — "I have seen up to here".

    EXACTLY ONE cursor, under the names secretarIA's own contract uses
    (`schemas/internal.py::BrainMessageReadMark` there, and the staff route's body): the id of
    the last message the patient saw, or an instant that carries its offset. Checked HERE, with
    the same rules, so a malformed cursor is the patient's own 422 instead of an upstream 422
    this service would have to report as `product_error`.

    Like `PatientMessageIn`, it names NO tenant and NO patient: both come from the session, and
    `extra="forbid"` refuses a body that tries — which conversation gets marked read is never
    a patient's input.
    """

    model_config = ConfigDict(extra="forbid")

    up_to_message_id: UUID | None = None
    # A naive time is refused, never guessed at (`naive-timestamp-serialization`).
    up_to: AwareDatetime | None = None

    @model_validator(mode="after")
    def _exactly_one_cursor(self) -> "PatientReadMarkIn":
        if (self.up_to_message_id is None) == (self.up_to is None):
            raise ValueError("send exactly one of up_to_message_id, up_to")
        return self


class MessageOut(BaseModel):
    """A generic one-line message body (mirrors `schemas/auth.py::MessageOut`)."""

    detail: str


class RelayOut(BaseModel):
    """The sibling service's own turn payload, passed through unchanged.

    Deliberately NOT re-typed field by field. secretarIA answers `{"status": "queued"}`
    and PreCheck answers a whole rendered question with options; freezing either shape
    here would mean this repo has to ship every time one of them adds a field
    (`frozen-contract-migration` — a permissive envelope is what keeps a two-sided
    contract from needing a synchronized deploy). What brain-api DOES own is the outer
    envelope: `product` says which backend produced it, so the client never has to guess.
    """

    model_config = ConfigDict(extra="allow")

    product: str
    payload: dict
    at: datetime


# --- The PENDING visit (2026-09-16): chat first, e-mail later, code last -------------------
#
# A separate family of bodies on purpose. Nothing here carries an account, and the one thing
# that WOULD be dangerous to accept from a browser — the address being verified — is absent by
# design: it is claimed server-to-server by secretarIA from the conversation itself
# (`schemas/internal.py::PendingEmailClaimIn`), so a client cannot aim a code at an inbox it
# does not own.


class PublicProductsOut(BaseModel):
    """Per-product booleans, and nothing else (the pre-login shape of `ProductsOut`)."""

    secretaria: bool
    precheck: bool


class ClinicLookupIn(BaseModel):
    """`POST /patient-access/clinics/lookup` — what the portal knows before any login.

    Body rather than a query string for the same reason the sibling invite routes use one: the
    invite may be a whole pasted URL, and a URL inside a URL lands in every access log on the
    way. The invite is not a credential (`core/invite_codes.py` says why), but there is no
    reason to write it down either.
    """

    model_config = ConfigDict(extra="forbid")

    invite: str = Field(min_length=1, max_length=MAX_INVITE_LENGTH)


class ClinicPublicOut(BaseModel):
    """The pre-login answer about ONE clinic: its name, and which products it offers here.

    Deliberately three fields. `products` is `EntitlementOut.products` INTERSECTED with the
    Brain-Message channel (`services/message_switchboard.py::available_products`), which is the
    same computation `GET /patient-access/threads` does after login — so the toggle a visitor
    sees before logging in cannot disagree with the tabs they get after. Plan, status, limits,
    usage and add-ons stay behind the staff token: what a clinic PAYS is not a visitor's
    business, and `false` for a product it never bought looks identical to `false` for one
    whose subscription lapsed.

    `clinic_name` is here because the portal must be able to say whose chat this is, and the
    invite link already names the clinic to whoever holds it.
    """

    tenant_id: UUID
    clinic_name: str
    products: PublicProductsOut


class PendingSessionIn(BaseModel):
    """`POST /patient-access/pending` — open (or resume) a conversation with no login.

    `product` is what the visitor arrived for: the secretarIA link, or the clinic's DIRECT
    PreCheck link. It is checked against the clinic's entitlements HERE so a dead link fails
    at the door with a clear answer, instead of at the first message with a 403 from the relay.
    Optional, defaulting to secretarIA, because that is the link the clinic hands out by default.
    """

    model_config = ConfigDict(extra="forbid")

    invite: str = Field(min_length=1, max_length=MAX_INVITE_LENGTH)
    product: str | None = Field(default=None, min_length=1, max_length=32)


class PendingSessionOut(BaseModel):
    """What a pending visit hands the browser: a token, a handle, and what the clinic offers.

    The opaque leg is NOT in this body — it is the `__Host-patient_pending` cookie, for the
    same reason the account's is a cookie (`core/cookies.py`). What IS here is a 30-minute
    scoped JWT for page memory, plus the facts the chat screen needs to render itself.
    """

    # WHICH session this answer carries (2026-09-24,
    # docs/CHECKPOINT_portal_sessao_ativa_pula_pendente.md):
    # - "pending": a visit, as before — `pending_token` is set, `access_token` is null;
    # - "account": the browser carried a live ACCOUNT cookie (plus `X-Brain-Client`), so the
    #   clinic was added to that account and no visit exists — `access_token` is an ordinary
    #   CLINIC token (scope `patient_message`, renewable through `/refresh` like any clinic
    #   of the account), `pending_token` is null, and there is no e-mail or code to ask for.
    # Read this field; never infer the kind from which token happens to be present.
    session_kind: Literal["pending", "account"] = "pending"
    # Scope `patient_pending`: the thread routes of exactly this clinic, and nothing else.
    pending_token: str | None = None
    # Scope `patient_message`, bound to the account's login row (`sid` = `login_sid`).
    access_token: str | None = None
    token_type: str = "bearer"
    expires_in: int
    tenant_id: UUID
    clinic_name: str
    # `MessagePatient.id` — already the handle secretarIA and PreCheck will store, and the one
    # the account will keep. Named `patient_ref` like everywhere else in this contract.
    patient_ref: UUID
    products: PublicProductsOut
    # Whether secretarIA has already captured an address on this visit. The portal uses it to
    # decide whether "enviar código" is even offered yet. The address itself is never returned:
    # echoing it back would turn this route into a way to read what another visit captured.
    # Always false for `session_kind="account"`: there is no visit and no code to offer.
    email_claimed: bool = False


class PendingProgressOut(BaseModel):
    """Read-only progress for the chat UI; contains no address, code or token.

    `otp_sent` is visit-local (`MessagePendingSession.otp_requested_at`), so an account
    login challenge requested elsewhere for the same inbox cannot switch this composer into
    code mode. `verified` means the service leg accepted the code and `/pending/complete` is
    now allowed.
    """

    state: Literal["pending_unclaimed", "pending_claimed", "otp_sent", "verified"]


class PendingVerifyIn(BaseModel):
    """`POST /patient-access/pending/verify-otp` — the code, and only the code.

    NO e-mail field, and that is the security property, not an omission: the address verified
    is the one the CONVERSATION captured (`message_pending_sessions.email`). A body that could
    name an address would let anyone holding a pending token point a code at any inbox.
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=32)
