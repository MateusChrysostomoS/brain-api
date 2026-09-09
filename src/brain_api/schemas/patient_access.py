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


class PatientSessionOut(BaseModel):
    """What `verify-otp` returns: the in-memory access leg + who it belongs to.

    The revocable leg is NOT in this body — it is set as the `__Host-patient_session`
    HttpOnly cookie, which is the entire point of the split (see `core/cookies.py`).
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    tenant_id: UUID
    # `MessagePatient.id` — the same handle secretarIA and PreCheck know this patient by.
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
