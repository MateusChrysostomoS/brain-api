"""Auth endpoints (CONTRACTS.md §2): login + current identity.

`POST /auth/token` mints a brain-api JWT carrying only stable identity
(`sub`/`tenant_id`/`role`) — no entitlements, no secrets (auth-jwt-multitenant rule).
`GET /auth/me` returns identity-only response models that whitelist non-sensitive
fields, so `password_hash` can never be serialized (tenant-secrets-encryption rule).

The Authorization header, the token and the password are NEVER logged; login success
logs only a stable `user_id` reference.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, get_current_principal
from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.core.security import create_access_token
from brain_api.schemas.auth import (
    LoginRequest,
    MeResponse,
    TenantOut,
    TokenResponse,
    UserOut,
)
from brain_api.services.auth import authenticate, get_tenant, get_user

logger = get_logger(__name__)

router = APIRouter(prefix="/auth")


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Log in",
    description="Exchange email + password for a brain-api access token.",
    responses={
        401: {"description": "Unknown email or bad password."},
        422: {"description": "Malformed email or password longer than 72 bytes."},
    },
)
async def login(
    payload: LoginRequest,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Authenticate the credentials and mint a short-lived access token."""
    user = await authenticate(session, payload.email, payload.password)
    if user is None:
        # Same message for unknown email and bad password — do not distinguish.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais inválidas",
        )
    token = create_access_token(
        sub=str(user.id),
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
        role=user.role,
    )
    # Stable reference only — never log the email, password or token.
    logger.info("login", user_id=str(user.id))
    return TokenResponse(access_token=token)


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
