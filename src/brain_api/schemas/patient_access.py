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
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

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
