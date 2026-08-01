"""Pydantic v2 schemas for the doctor (tenant) vertical (RBAC task, Part 1B).

`GET /doctor/me` returns the doctor's identity + their tenant + the tenant's resolved
entitlements (which products they may use). It reuses the whitelisted identity schemas
(`UserOut`, `TenantOut`) and the entitlement view (`EntitlementOut`), so no secret or
`password_hash` can be serialized here either.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from brain_api.schemas.auth import TenantOut, UserOut
from brain_api.schemas.entitlement import EntitlementOut


class DoctorMeOut(BaseModel):
    """`GET /doctor/me` — the doctor's profile, tenant, and entitlement state.

    `tenant` is always present (a doctor is tenant-scoped). `entitlements` tells the
    portal which products to surface (PreCheck / SecretarIA).
    """

    user: UserOut
    tenant: TenantOut
    entitlements: EntitlementOut


class DoctorMeUpdateIn(BaseModel):
    """`PATCH /doctor/me` body — self-edit of the CALLER'S OWN low-risk identity fields.

    `name` is the ONLY editable field here. Email (login key + PreCheck SSO identity via
    the shared-SECRET_KEY handoff), role, tenant, and password are deliberately NOT
    accepted — `extra="forbid"` means a request that includes any of them is rejected
    with `422` by the schema itself, never by a manual field filter that could drift.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)

    @field_validator("name")
    @classmethod
    def _trim(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class HubTokenOut(BaseModel):
    """`POST /doctor/secretaria/hub-token` — the minted hub session.

    `hub_token` is purpose-scoped (`scope=secretaria_hub`, tenant in `sub`) and is NOT
    a brain user JWT: brain-api's own auth rejects it, and secretarIA accepts it only
    after live introspection. Deliberately not named `access_token` so no client ever
    stores it in the brain-session slot.
    """

    hub_token: str
    token_type: str = "bearer"
    expires_in: int
