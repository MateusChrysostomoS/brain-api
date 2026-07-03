"""Pydantic v2 schemas for the doctor (tenant) vertical (RBAC task, Part 1B).

`GET /doctor/me` returns the doctor's identity + their tenant + the tenant's resolved
entitlements (which products they may use). It reuses the whitelisted identity schemas
(`UserOut`, `TenantOut`) and the entitlement view (`EntitlementOut`), so no secret or
`password_hash` can be serialized here either.
"""

from pydantic import BaseModel

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
