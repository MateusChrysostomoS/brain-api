"""Pydantic v2 schemas for the launch-waitlist vertical (CONTRACTS.md §4 + §5).

Public lead capture behind the PRE-LAUNCH GATE: while the frontend's `PRODUCT_LAUNCHED`
flag is false, every buy button on the pricing page opens a "em breve" modal instead of
starting checkout, and that modal posts here.

Mirrors `schemas.demo` deliberately — same honeypot field (`website`, never persisted,
CONTRACTS.md §5), same trim-and-reject-blank name validator. The only real difference is
`plan_hint`, a free-form record of WHICH purchase the blocked click was for.
"""

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator


class WaitlistLeadCreate(BaseModel):
    """Body for `POST /public/launch-waitlist`.

    `plan_hint` is deliberately NOT validated against `services.catalog`: it is a sales
    hint captured today for a catalog that may well have changed by launch day, so an
    unknown id must never 422 a lead out of the list.
    """

    name: str = Field(min_length=1, max_length=255)
    email: EmailStr = Field(max_length=320)
    plan_hint: str | None = Field(default=None, max_length=255)
    # HONEYPOT (anti-spam, CONTRACTS.md §5). A real browser leaves this hidden field
    # empty; a bot fills it. It is NEVER persisted — the API layer accept-and-drops when
    # it is non-empty.
    website: str | None = None

    @field_validator("name")
    @classmethod
    def _trim_name(cls, v: str) -> str:
        """Trim `name` and reject blank-after-trim (422)."""
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("plan_hint")
    @classmethod
    def _trim_plan_hint(cls, v: str | None) -> str | None:
        """Trim `plan_hint`; blank-after-trim stores as NULL rather than an empty string."""
        if v is None:
            return None
        v = v.strip()
        return v or None


class WaitlistLeadConfirmation(BaseModel):
    """`POST /public/launch-waitlist` response. No lead data echoed back.

    A re-submission of an already-registered email returns the SAME `id` as the first
    one (the endpoint is idempotent per email) with identical copy — the visitor is
    never told whether they were already on the list.
    """

    id: UUID
    message: str
