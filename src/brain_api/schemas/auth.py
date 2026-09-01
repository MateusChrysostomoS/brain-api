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

    `email` is ADDITIVE for the same reason, added by the HttpOnly-cookie round
    (core/cookies.py). It matters now in a way it did not before: a portal that
    resumes its session from the refresh COOKIE never saw a login form, so unlike
    `login()` it has no submitted address to fall back on — and the access token
    deliberately carries no email claim (auth-jwt-multitenant: the JWT holds
    stable identity, not display data). Returning it here is not a disclosure:
    every route that fills it in has already authenticated the caller AS that
    user.
    """

    access_token: str
    token_type: str = "bearer"
    refresh_token: str | None = None
    expires_in: int | None = None
    name: str | None = None
    email: str | None = None
    professional_id: UUID | None = None


class RefreshRequest(BaseModel):
    """`POST /auth/refresh` body — exchange a refresh token for a new session pair.

    `refresh_token` is OPTIONAL because the token normally arrives in the HttpOnly
    `__Host-refresh_token` cookie instead (core/cookies.py), which the browser
    attaches on its own — a migrated portal posts no body at all. The field stays
    for the clients that have not migrated yet; the route prefers the cookie and
    answers 401 when neither leg is present. Do NOT make it required again until
    every frontend is confirmed migrated in production.
    """

    refresh_token: str | None = Field(default=None, min_length=1, max_length=512)


class LogoutRequest(BaseModel):
    """`POST /auth/logout` body — revoke a refresh token (ends the revocable leg).

    Optional for the same reason as `RefreshRequest.refresh_token` above. Logout
    revokes the cookie's token AND the body's when both are present, so a client
    mid-migration can never leave one of the two legs alive.
    """

    refresh_token: str | None = Field(default=None, min_length=1, max_length=512)


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


# --- Password reset (CONTRACTS.md §2.6) -------------------------------------
#
# Request/response shapes mirror PreCheck's long-standing reset endpoints
# (app/schemas/auth.py there) ON PURPOSE: brain-frontend's three `esqueci_senha/*`
# screens were already written against that contract while pointed at the wrong
# backend, so matching it exactly means repointing them is an import swap rather
# than a UI rewrite.


class MessageOut(BaseModel):
    """Generic `{detail}` envelope, mirroring PreCheck's `MessageResponse`.

    Used where the response must be intentionally indistinguishable between the
    success and the no-op case (see `PasswordResetRequestIn`), so the body carries
    no signal a caller could use to probe for registered emails.
    """

    detail: str


class PasswordResetRequestIn(BaseModel):
    """Body for `POST /auth/password-reset/request`.

    The endpoint ALWAYS answers 200 with the same `MessageOut`, whether or not the
    email belongs to a user — anything else would turn this route into an account
    enumeration oracle.
    """

    model_config = ConfigDict(extra="forbid")

    email: EmailStr = Field(max_length=320)


class PasswordResetVerifyIn(BaseModel):
    """Body for `POST /auth/password-reset/verify` — a read-only pre-flight so the UI
    can reject a broken link before the user types a new password. Does NOT consume
    the token."""

    model_config = ConfigDict(extra="forbid")

    # 32-byte url-safe tokens are ~43 chars; the ceiling tolerates future tweaks.
    token: str = Field(min_length=16, max_length=256)


class PasswordResetConfirmIn(BaseModel):
    """Body for `POST /auth/password-reset/confirm` — consumes the token and sets the
    new password.

    Same composition policy as `SetPasswordIn` / `AdminUserCreateIn` /
    `SignupIntentCreate`: 8-72 chars (bcrypt's ceiling), at least one letter and one
    digit. Reset is not a back door around the rule the account was created under.
    """

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=16, max_length=256)
    new_password: str = Field(min_length=8, max_length=72)

    @field_validator("new_password")
    @classmethod
    def _password_policy(cls, v: str) -> str:
        if not any(c.isalpha() for c in v) or not any(c.isdigit() for c in v):
            raise ValueError("password must contain at least one letter and one digit")
        return v
