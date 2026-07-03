"""Pydantic v2 schemas for brain-api's INBOUND /internal/* surface (secretarIA-facing).

Service-to-service payloads only (X-Internal-Api-Key callers — today secretarIA).
Nothing here carries a secret, a password hash, or a raw token back out: the hub token
arrives in the request body, is validated in-memory, and only booleans/ids leave.
"""

import re
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
    """

    active: bool
    tenant_id: UUID | None = None


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
    8-15 chars — the same shape PreCheck's own contract expects."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    phone_number: str = Field(min_length=8, max_length=15)

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
