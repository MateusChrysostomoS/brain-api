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

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from brain_api.services import catalog


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


class AdminTenantDeleteOut(BaseModel):
    """Result of deleting a clinic in cascade (DELETE /admin/tenants/{id}).

    `deleted` counts the brain-owned rows removed, per table. `secretaria.status` reports
    the cross-DB leg into secretaria's own database: `deleted` (its tenant row was
    removed), `absent` (it never had one — a PreCheck-only clinic), `kept_has_data`
    (secretaria refused because conversation history exists), `skipped_unconfigured`
    (secretaria not wired on brain-api), or `failed` (retryable error). A non-`deleted`
    secretaria status never means the brain deletion failed — that already committed.
    """

    tenant_id: UUID
    deleted: dict[str, int]
    secretaria: dict[str, str]


class EntitlementPatchIn(BaseModel):
    """Partial update of a tenant's entitlement (admin manual activation, pre-Stripe).

    Every field is optional — only those present are applied. This is how a product/plan
    is switched on for a tenant before Stripe exists. All values are validated against
    the catalog (`services/catalog.py`): `plan` must be an assignable catalog plan,
    `addons` keys must be known add-on ids (bool values), `limits` keys must be known
    limit keys (non-negative ints). Setting `plan` re-materializes products/addons/limits
    from the catalog; explicit fields in the same patch override that (see the service).
    """

    model_config = ConfigDict(extra="forbid")

    precheck_enabled: bool | None = None
    secretaria_enabled: bool | None = None
    plan: str | None = Field(default=None, max_length=32)
    status: Literal["active", "trialing", "past_due", "canceled", "inactive"] | None = None
    addons: dict[str, bool] | None = None
    limits: dict[str, int] | None = None

    @field_validator("plan")
    @classmethod
    def _plan_in_catalog(cls, v: str | None) -> str | None:
        """Only assignable catalog plans may be written (legacy aliases normalize).

        A reserved slot (available=False, e.g. a future second tier) would be rejected
        until product defines it — no tenant may be put on a plan with no feature set.
        The catalog has no such slot today (single secretaria_basico plan).
        """
        if v is None:
            return v
        plan = catalog.get_plan(v)
        if plan is None or plan.id not in catalog.ASSIGNABLE_PLAN_IDS:
            raise ValueError(
                f"unknown or unassignable plan {v!r}; "
                f"assignable: {sorted(catalog.ASSIGNABLE_PLAN_IDS)}"
            )
        return plan.id

    @field_validator("addons")
    @classmethod
    def _addon_keys_in_catalog(cls, v: dict[str, bool] | None) -> dict[str, bool] | None:
        if v is not None and (unknown := set(v) - catalog.ADDON_IDS):
            raise ValueError(f"unknown addon ids: {sorted(unknown)}")
        return v

    @field_validator("limits")
    @classmethod
    def _limit_keys_in_catalog(cls, v: dict[str, int] | None) -> dict[str, int] | None:
        if v is not None:
            if unknown := set(v) - catalog.LIMIT_KEYS:
                raise ValueError(f"unknown limit keys: {sorted(unknown)}")
            if negative := [k for k, n in v.items() if n < 0]:
                raise ValueError(f"limits must be >= 0: {sorted(negative)}")
        return v


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
    # Policy: 8–72 chars (bcrypt's 72-byte ceiling), at least one letter and one digit.
    password: str = Field(min_length=8, max_length=72)
    role: Literal["admin", "tenant_owner", "tenant_staff"]
    # Required for tenant roles; must be absent for a platform admin (validated below).
    tenant_id: UUID | None = None

    @field_validator("password")
    @classmethod
    def _password_policy(cls, v: str) -> str:
        """Minimum composition: length is enforced by the Field; require letter + digit
        so a purely-numeric or purely-alphabetic password is rejected (422)."""
        if not any(c.isalpha() for c in v) or not any(c.isdigit() for c in v):
            raise ValueError("password must contain at least one letter and one digit")
        return v

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
