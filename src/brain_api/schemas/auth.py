"""Pydantic v2 schemas for the auth vertical (CONTRACTS.md §2).

Response models WHITELIST non-sensitive fields (tenant-secrets-encryption never-leak
rule): there is NO `*Out` schema that declares `password_hash` or any secret column.
`ConfigDict(extra="ignore")` means an accidentally-passed secret attribute is dropped,
not serialized.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    """Credentials for `POST /auth/token`."""

    email: EmailStr = Field(max_length=320)
    # bcrypt truncates at 72 bytes, so a longer password must be rejected (422)
    # rather than silently compared against its first 72 bytes.
    password: str = Field(min_length=1, max_length=72)


class TokenResponse(BaseModel):
    """The brain-api access token. Shape mirrors PreCheck's `TokenResponse` and the
    frontend's existing `LoginResponse` (client stores `access_token` unchanged)."""

    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    """Identity-only view of a user. NEVER declares `password_hash` (or any secret)."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: UUID
    email: str
    name: str
    role: str


class TenantOut(BaseModel):
    """Identity-only view of a tenant (non-sensitive config)."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: UUID
    clinic_name: str


class MeResponse(BaseModel):
    """`GET /auth/me` payload — identity only (no entitlements, no secrets).

    `tenant` is `null` for a platform admin (no `tenant_id`).
    """

    user: UserOut
    tenant: TenantOut | None
