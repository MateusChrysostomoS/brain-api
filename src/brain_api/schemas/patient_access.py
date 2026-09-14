"""Schemas for the patient side of the Brain-Message channel.

The clinic is named by `tenant_id`, NOT by a slug. brain-api has no slug: `tenants` is
keyed by UUID, `EntitlementOut.tenant_id` is a UUID, secretarIA's internal routes take
`tenant_id: UUID` and PreCheck's take the brain tenant id as a string. Minting a second
public identifier would mean a new column, a uniqueness policy, a backfill and a
rename-collision story, all so that the one handle every other surface already uses could
be spelled twice. The UUID is opaque and unguessable, which is the property a public
identifier actually needs here.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class OtpRequestIn(BaseModel):
    """`POST /patient-access/request-otp`."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    # `EmailStr` (the repo already depends on email-validator) so a malformed address is
    # a 422 here rather than a delivery failure three hops later.
    email: EmailStr


class OtpVerifyIn(BaseModel):
    """`POST /patient-access/verify-otp`."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    email: EmailStr
    # Bounded so a multi-megabyte "code" cannot be posted at the hashing path; the real
    # check is the constant-time comparison, which a wrong LENGTH fails like any other
    # wrong value.
    code: str = Field(min_length=1, max_length=32)


class SiblingCandidateOut(BaseModel):
    """Another clinic where the SAME address is already a Brain-Message patient.

    A question for the patient, not a session: there is deliberately NO token here. The
    only way to one is `POST /patient-access/siblings/{tenant_id}/confirm`, after the
    patient confirms — by name — that this clinic's conversation is theirs. An address
    alone never links two clinics: it can be shared by a family or reassigned.
    """

    tenant_id: UUID
    clinic_name: str
    # True when this account already confirmed this clinic in an earlier login (a
    # `brain_message_account_link` consent event exists). The client may then call confirm
    # without asking again — the call is idempotent and records nothing new.
    already_linked: bool = False


class PatientSessionOut(BaseModel):
    """What `verify-otp` returns: the in-memory access leg, whose it is, and the other
    clinics that know the same address.

    The revocable leg is NOT in this body — it is set as the `__Host-patient_session`
    HttpOnly cookie, which is the entire point of the split (see `core/cookies.py`).
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    tenant_id: UUID
    # `MessagePatient.id` — the same handle secretarIA and PreCheck know this patient by.
    patient_ref: UUID
    # The clinic this session opens, so a client holding several sessions can label each
    # one without another round trip (`/threads` has no name to give when it is empty).
    clinic_name: str
    # Clinics to ASK about, without tokens (see `SiblingCandidateOut`). Empty for a patient
    # of a single clinic — today's body plus one empty list.
    sibling_candidates: list[SiblingCandidateOut] = Field(default_factory=list)


class ConfirmSiblingIn(BaseModel):
    """`POST /patient-access/siblings/{tenant_id}/confirm`.

    Names the clinic being confirmed AGAIN, and the router refuses a body that disagrees
    with the path. The echo makes the request a statement ("I confirm THIS clinic")
    rather than a bare POST to a URL, so a client bug that confirms the wrong row fails
    loudly instead of linking silently. Either way the id is only an input to a lookup
    scoped by the AUTHENTICATED address — never an authority.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID


class ClinicSessionOut(BaseModel):
    """A session at a clinic the patient linked by confirmation.

    The same access leg as `PatientSessionOut` (a scoped JWT for ONE tenant, held in
    memory) with the same kind of server-side row behind it, which is what a logout
    revokes. What it lacks is a cookie: `__Host-patient_session` is one flat cookie and
    stays the login session's, so this session's revocable leg is its row alone (`sid`).
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    tenant_id: UUID
    clinic_name: str
    patient_ref: UUID


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
