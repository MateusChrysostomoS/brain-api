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
