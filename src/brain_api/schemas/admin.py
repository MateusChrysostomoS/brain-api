"""Pydantic v2 schemas for the platform-admin vertical (RBAC task, Part 1A).

Every response model WHITELISTS non-sensitive fields (tenant-secrets-encryption
never-leak rule). No `*Out` here declares `password_hash`, any `*_encrypted` column, or
any other secret — so an admin listing can never serialize one even if a raw ORM row is
handed to the wrong serializer (`extra="ignore"` drops stray attributes).

These endpoints are reachable ONLY by a platform `admin` JWT (router-level
`require_role("admin")`); see `api/admin.py`.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class Page[T](BaseModel):
    """A uniform paginated envelope: `items` plus the resolved window + total count.

    `skip`/`limit` echo the request so the client can render pagination without
    re-deriving them. Used by every admin list endpoint.
    """

    items: list[T]
    total: int
    skip: int
    limit: int


# --- Tenants ---------------------------------------------------------------


class AdminTenantOut(BaseModel):
    """A tenant row for the admin tenants table.

    Assembled by the service (joins the entitlement flags + a user count); not a 1:1
    attribute map, so it is constructed explicitly rather than via `from_attributes`.
    Carries NO credentials (the `tenants` table holds none; secrets would live in a
    separate `tenant_credentials` table that does not exist here).
    """

    id: UUID
    clinic_name: str
    created_at: datetime
    plan: str
    status: str
    precheck_enabled: bool
    secretaria_enabled: bool
    users_count: int


class EntitlementAdminOut(BaseModel):
    """Full entitlement record for one tenant (admin read).

    The Stripe linkage ids are scaffold identifiers (not secrets) and are surfaced for
    the admin; no token, key, or `*_encrypted` value is present on this model.
    """

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    tenant_id: UUID
    precheck_enabled: bool
    secretaria_enabled: bool
    plan: str
    status: str
    addons: dict = Field(default_factory=dict)
    limits: dict = Field(default_factory=dict)
    usage: dict = Field(default_factory=dict)
    period_start: datetime | None = None
    period_end: datetime | None = None
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    updated_at: datetime | None = None


class AdminTenantDetailOut(BaseModel):
    """Full tenant detail (admin). Embeds the resolved entitlement record."""

    id: UUID
    clinic_name: str
    created_at: datetime
    updated_at: datetime
    users_count: int
    entitlements: EntitlementAdminOut


class EntitlementPatchIn(BaseModel):
    """Partial update of a tenant's entitlement (admin manual activation, MVP).

    Every field is optional — only those present are applied. This is how PreCheck or
    SecretarIA is switched on for a tenant before Stripe exists. `addons`/`limits` accept
    a full replacement object when present.
    """

    model_config = ConfigDict(extra="forbid")

    precheck_enabled: bool | None = None
    secretaria_enabled: bool | None = None
    plan: str | None = Field(default=None, max_length=32)
    status: Literal["active", "trialing", "past_due", "canceled", "inactive"] | None = None
    addons: dict | None = None
    limits: dict | None = None


# --- Users -----------------------------------------------------------------


class AdminUserOut(BaseModel):
    """A user row for the admin users table. NEVER declares `password_hash`.

    `clinic_name` is `None` for a platform admin (no tenant); the portal renders that as
    "Platform Admin". Built by the service (joins the tenant name).
    """

    id: UUID
    tenant_id: UUID | None
    clinic_name: str | None
    email: str
    name: str
    role: str
    created_at: datetime


class AdminUserCreateIn(BaseModel):
    """Create a user in any tenant, with any role (admin tooling, Part 1A).

    Password is bcrypt-hashed by the service and never echoed back. bcrypt truncates at
    72 bytes, so a longer password is rejected (422) rather than silently truncated.
    """

    email: EmailStr = Field(max_length=320)
    name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=72)
    role: Literal["admin", "tenant_owner", "tenant_staff"]
    # Required for tenant roles; must be absent for a platform admin (validated below).
    tenant_id: UUID | None = None

    @model_validator(mode="after")
    def _check_role_tenant_consistency(self) -> "AdminUserCreateIn":
        """A platform admin has no tenant; a tenant user must name one."""
        if self.role == "admin":
            if self.tenant_id is not None:
                raise ValueError("admin users are platform-level and take no tenant_id")
        elif self.tenant_id is None:
            raise ValueError("tenant_owner/tenant_staff users require a tenant_id")
        return self


# --- Demo requests ---------------------------------------------------------


class AdminDemoRequestOut(BaseModel):
    """A demo/lead row for the admin demo-requests table (brain's own table)."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: UUID
    name: str
    email: str
    clinic: str | None
    profile: str | None
    product_interest: str | None
    message: str | None
    source: str | None
    status: str
    created_at: datetime


class AdminDemoRequestPatchIn(BaseModel):
    """Move a demo request along the pipeline.

    The portal's three row actions ("Marcar como contatado", "Converter em tenant",
    "Descartar") map to these three target statuses. `new` is the initial state and is
    not a valid PATCH target.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["contacted", "converted", "dismissed"]


# --- Doctor-mode impersonation ("Modo médico") -----------------------------


class ImpersonationTokenOut(BaseModel):
    """A minted doctor session for the admin "Modo médico" handoff.

    `access_token` is shape-identical to that doctor's own `/auth/token` login (claims
    `sub`=doctor user / `tenant_id` / `role`), so the doctor portal + PreCheck SSO accept
    it UNCHANGED — the admin literally acts as that clinic's user. The remaining fields are
    non-secret display data the portal shows in the "you are in doctor mode" banner; no
    `password_hash` or other secret ever rides along.
    """

    access_token: str
    token_type: str = "bearer"
    tenant_id: UUID
    clinic_name: str
    email: str
    role: str
    expires_in: int
