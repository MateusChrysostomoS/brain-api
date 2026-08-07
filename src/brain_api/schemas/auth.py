"""Pydantic v2 schemas for the auth vertical (CONTRACTS.md §2).

Response models WHITELIST non-sensitive fields (tenant-secrets-encryption never-leak
rule): there is NO `*Out` schema that declares `password_hash` or any secret column.
`ConfigDict(extra="ignore")` means an accidentally-passed secret attribute is dropped,
not serialized.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class LoginRequest(BaseModel):
    """Credentials for `POST /auth/token`."""

    email: EmailStr = Field(max_length=320)
    # bcrypt truncates at 72 bytes, so a longer password must be rejected (422)
    # rather than silently compared against its first 72 bytes.
    password: str = Field(min_length=1, max_length=72)


class TokenResponse(BaseModel):
    """The brain-api session pair. Shape mirrors PreCheck's `TokenResponse` and the
    frontend's existing `LoginResponse` (client stores `access_token` unchanged);
    `refresh_token`/`expires_in` are ADDITIVE fields (auth-hardening round) that
    existing consumers ignore.

    `refresh_token` is the opaque, revocable long-lived leg — returned exactly once
    per issue/rotation (only its hash is stored server-side). `expires_in` is the
    ACCESS token's lifetime in seconds.

    `name`/`professional_id` are ADDITIVE identity fields (onboarding/multi-professional
    round, CONTRACT_onboarding_v1.md §6) so the portal has them right after login/refresh
    without a separate `/auth/me` round-trip; `professional_id` is also embedded as a
    claim on `access_token` itself (see `core.security.create_access_token`).
    """

    access_token: str
    token_type: str = "bearer"
    refresh_token: str | None = None
    expires_in: int | None = None
    name: str | None = None
    professional_id: UUID | None = None


class RefreshRequest(BaseModel):
    """`POST /auth/refresh` body — exchange a refresh token for a new session pair."""

    refresh_token: str = Field(min_length=1, max_length=512)


class LogoutRequest(BaseModel):
    """`POST /auth/logout` body — revoke a refresh token (ends the revocable leg)."""

    refresh_token: str = Field(min_length=1, max_length=512)


class UserOut(BaseModel):
    """Identity-only view of a user. NEVER declares `password_hash` (or any secret)."""

    model_config = ConfigDict(from_attributes=True, extra="ignore")

    id: UUID
    email: str
    name: str
    role: str
    professional_id: UUID | None = None


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


class ExchangeOnboardingTokenIn(BaseModel):
    """`POST /auth/exchange-onboarding-token` body — redeem the ONE-TIME token minted by
    `GET /public/onboarding-status` for a real session pair (services/signup.py)."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=512)


class ExchangeInviteTokenIn(BaseModel):
    """`POST /auth/exchange-invite-token` body — redeem the professional-invite token
    minted by `POST /doctor/professionals/invites` for a real session pair. Mirrors
    `ExchangeOnboardingTokenIn` exactly (CONTRACT_onboarding_v1.md §6)."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=512)


class SetPasswordIn(BaseModel):
    """`POST /auth/set-password` body — the caller replaces their OWN password.

    Used by the professional-invite accept flow (`/convite`): an invited `doctor`
    (non-owner) user starts on a random, never-communicated password and sets a real one
    here right after redeeming the invite token. (Cold-signup OWNERS no longer need this —
    they choose a real password at registration, `services/signup.register_signup`.) Same
    composition policy as admin-created users (`schemas/admin.py` `AdminUserCreateIn`):
    8-72 chars (bcrypt's ceiling), at least one letter and one digit.
    """

    model_config = ConfigDict(extra="forbid")

    new_password: str = Field(min_length=8, max_length=72)

    @field_validator("new_password")
    @classmethod
    def _password_policy(cls, v: str) -> str:
        if not any(c.isalpha() for c in v) or not any(c.isdigit() for c in v):
            raise ValueError("password must contain at least one letter and one digit")
        return v
