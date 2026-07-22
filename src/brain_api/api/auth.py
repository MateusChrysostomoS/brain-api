"""Auth endpoints (CONTRACTS.md §2): login + current identity.

`POST /auth/token` mints a brain-api JWT carrying only stable identity
(`sub`/`tenant_id`/`role`) — no entitlements, no secrets (auth-jwt-multitenant rule).
`GET /auth/me` returns identity-only response models that whitelist non-sensitive
fields, so `password_hash` can never be serialized (tenant-secrets-encryption rule).

The Authorization header, the token and the password are NEVER logged; login success
logs only a stable `user_id` reference.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, get_current_principal
from brain_api.config import get_settings
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.core.ratelimit import SlidingWindowLimiter, client_ip
from brain_api.core.security import create_access_token
from brain_api.models import User
from brain_api.schemas.auth import (
    ExchangeInviteTokenIn,
    ExchangeOnboardingTokenIn,
    LoginRequest,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    SetPasswordIn,
    TenantOut,
    TokenResponse,
    UserOut,
)
from brain_api.services import signup as signup_service
from brain_api.services.auth import (
    authenticate,
    exchange_invite_token as _exchange_invite_token,
    get_tenant,
    get_user,
    issue_refresh_token,
    revoke_refresh_token,
    rotate_refresh_token,
    set_password as _set_password,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/auth")

# One bucket for the credential-bearing endpoints (login + refresh) — blunts credential
# stuffing / token brute force. In-process + fail-open per CONTRACTS.md §5.
_limiter = SlidingWindowLimiter("auth", lambda: get_settings().AUTH_RATE_LIMIT_PER_MIN)


def _check_auth_rate_limit(request: Request) -> None:
    """429 when the client IP exceeds the per-minute auth budget (fail-open inside)."""
    if not _limiter.allow(client_ip(request)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Try again in a minute.",
        )


def build_session_response(user: User, refresh_token: str) -> TokenResponse:
    """Assemble the access+refresh response for a (re)authenticated user.

    The single place the session pair (access JWT + opaque refresh) is built, reused by
    every path that mints a session: login, refresh, the two token exchanges here, AND the
    cold-signup registration in `api/public_signup.py` (imported from there) so a freshly
    registered lead gets a session byte-identical to a normal login.
    """
    return TokenResponse(
        access_token=create_access_token(
            sub=str(user.id),
            tenant_id=str(user.tenant_id) if user.tenant_id else None,
            role=user.role,
            professional_id=str(user.professional_id) if user.professional_id else None,
        ),
        refresh_token=refresh_token,
        expires_in=get_settings().ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        name=user.name,
        professional_id=user.professional_id,
    )


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Log in",
    description="Exchange email + password for a brain-api access token.",
    responses={
        401: {"description": "Unknown email or bad password."},
        422: {"description": "Malformed email or password longer than 72 bytes."},
        429: {"description": "Rate limited (per-IP auth budget)."},
    },
)
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Authenticate the credentials and mint the access + refresh session pair."""
    _check_auth_rate_limit(request)
    user = await authenticate(session, payload.email, payload.password)
    if user is None:
        # Same message for unknown email and bad password — do not distinguish.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais inválidas",
        )
    refresh = await issue_refresh_token(session, user.id)
    # Stable reference only — never log the email, password or either token.
    logger.info("login", user_id=str(user.id))
    return build_session_response(user, refresh)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh the session",
    description="Rotate a refresh token: the presented one is revoked, a new pair is issued.",
    responses={
        401: {"description": "Unknown, expired, revoked (or reused) refresh token."},
        429: {"description": "Rate limited (per-IP auth budget)."},
    },
)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Rotate-on-use: one refresh token yields exactly one successor. Reuse of an
    already-rotated token revokes the user's whole refresh family (theft signal)."""
    _check_auth_rate_limit(request)
    result = await rotate_refresh_token(session, payload.refresh_token)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )
    logger.info("token_refreshed", user_id=str(result.user.id))
    return build_session_response(result.user, result.new_refresh_token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out",
    description="Revoke a refresh token. Always 204 (no token-existence oracle).",
)
async def logout(
    payload: LogoutRequest,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """End the revocable leg. The short-lived access token simply expires."""
    await revoke_refresh_token(session, payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/me",
    response_model=MeResponse,
    summary="Current identity",
    description="Return the authenticated user + tenant (identity only, no secrets).",
    responses={401: {"description": "Missing, invalid or expired token."}},
)
async def me(
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> MeResponse:
    """Resolve the principal back to its user (and tenant) rows, identity only."""
    # `principal.user_id` is the JWT `sub` — a UUID string.
    user = await get_user(session, UUID(principal.user_id))
    if user is None:
        # Token was valid but the user no longer exists (e.g. deleted after issue).
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    # None for a platform admin (no tenant context) -> tenant=None.
    tenant = await get_tenant(session, user.tenant_id) if user.tenant_id else None
    return MeResponse(
        user=UserOut.model_validate(user),
        tenant=TenantOut.model_validate(tenant) if tenant else None,
    )


@router.post(
    "/exchange-onboarding-token",
    response_model=TokenResponse,
    summary="Exchange a signup onboarding token for a session",
    description=(
        "Redeem the ONE-TIME token minted by GET /public/onboarding-status once a cold "
        "signup finishes provisioning (services/signup.py). Single-use: burned on success."
    ),
    responses={
        401: {"description": "Unknown, expired, already-used token, or unprovisioned intent."},
        429: {"description": "Rate limited (per-IP auth budget)."},
    },
)
async def exchange_onboarding_token(
    payload: ExchangeOnboardingTokenIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Mint the SAME session pair a password login would, for the provisioned owner."""
    _check_auth_rate_limit(request)
    result = await signup_service.exchange_onboarding_token(session, payload.token)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_onboarding_token",
        )
    refresh = await issue_refresh_token(session, result.user.id)
    # Stable references only — never the token.
    logger.info(
        "onboarding_token_exchanged",
        intent_id=str(result.intent_id),
        tenant_id=str(result.user.tenant_id),
    )
    return build_session_response(result.user, refresh)


@router.post(
    "/exchange-invite-token",
    response_model=TokenResponse,
    summary="Exchange a professional-invite token for a session",
    description=(
        "Redeem the token minted by POST /doctor/professionals/invites. Single-use: "
        "burned on success. Mirrors /auth/exchange-onboarding-token exactly."
    ),
    responses={
        401: {"description": "Unknown, expired, or already-used token."},
        429: {"description": "Rate limited (per-IP auth budget)."},
    },
)
async def exchange_invite_token(
    payload: ExchangeInviteTokenIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Mint the SAME session pair a password login would, for the invited professional."""
    _check_auth_rate_limit(request)
    user = await _exchange_invite_token(session, payload.token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_invite_token",
        )
    refresh = await issue_refresh_token(session, user.id)
    # Stable references only — never the token.
    logger.info(
        "invite_token_exchanged",
        user_id=str(user.id),
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
    )
    return build_session_response(user, refresh)


@router.post(
    "/set-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Set the caller's own password",
    description=(
        "Lets the authenticated caller replace their password — needed because a "
        "signup-provisioned tenant owner starts on a random, never-communicated one."
    ),
    responses={401: {"description": "Missing, invalid or expired token."}},
)
async def set_password(
    payload: SetPasswordIn,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The caller sets THEIR OWN password (never someone else's — scoped by the token)."""
    await _set_password(session, UUID(principal.user_id), payload.new_password)
    logger.info("password_set", user_id=principal.user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
