"""Auth endpoints (CONTRACTS.md §2): login + current identity.

`POST /auth/token` mints a brain-api JWT carrying only stable identity
(`sub`/`tenant_id`/`role`) — no entitlements, no secrets (auth-jwt-multitenant rule).
`GET /auth/me` returns identity-only response models that whitelist non-sensitive
fields, so `password_hash` can never be serialized (tenant-secrets-encryption rule).

The Authorization header, the token and the password are NEVER logged; login success
logs only a stable `user_id` reference.

THE REFRESH LEG TRAVELS IN A COOKIE (2026-08-31). Every route here that mints a
session pair also writes the opaque refresh token to the HttpOnly
`__Host-refresh_token` cookie — see `core/cookies.py` for why each attribute is
what it is. `POST /auth/refresh` and `POST /auth/logout` accept the token from
EITHER that cookie or the JSON body, preferring the cookie; the body leg is kept
only until both portals are confirmed migrated in production, and is what makes
this round purely additive for a client that has not moved yet.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.api.deps import Principal, get_current_principal
from brain_api.config import get_settings
from brain_api.core.cookies import (
    clear_refresh_cookie,
    read_refresh_cookie,
    require_client_header,
    set_refresh_cookie,
)
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
    MessageOut,
    PasswordResetConfirmIn,
    PasswordResetRequestIn,
    PasswordResetVerifyIn,
    RefreshRequest,
    SetPasswordIn,
    TenantOut,
    TokenResponse,
    UserOut,
)
from brain_api.services import secretaria_provisioning, signup as signup_service
from brain_api.services.auth import (
    authenticate,
    complete_password_reset as _complete_password_reset,
    exchange_invite_token as _exchange_invite_token,
    find_password_reset_user,
    get_tenant,
    get_user,
    issue_password_reset_token,
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

    Prefer `issue_session` below unless you genuinely have no `Response` to write
    the cookie onto — a session minted without the cookie can never be resumed
    after a reload, because the migrated portals keep the access token in memory
    only.
    """
    return TokenResponse(
        access_token=create_access_token(
            sub=str(user.id),
            tenant_id=str(user.tenant_id) if user.tenant_id else None,
            role=user.role,
            professional_id=str(user.professional_id) if user.professional_id else None,
            is_owner=user.is_owner,
            is_manager=user.is_manager,
        ),
        refresh_token=refresh_token,
        expires_in=get_settings().ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        name=user.name,
        email=user.email,
        professional_id=user.professional_id,
    )


def issue_session(response: Response, user: User, refresh_token: str) -> TokenResponse:
    """Build the session pair AND plant the refresh cookie on the outgoing response.

    Every route that hands a browser a brand-new session goes through here, so the
    cookie can never drift out of step with the token in the body — including
    after a ROTATION, where the cookie must carry the successor and never the
    token just spent.

    Deliberately NOT used by `POST /admin/impersonate/token` ("Modo médico"):
    that route mints an access token with no refresh leg at all, and overwriting
    the admin's own cookie with a tenant-scoped one — or leaving it in place for
    the impersonated session to silently rotate — would quietly swap who the
    browser is. The frontend marks that session non-refreshable for the same
    reason.
    """
    set_refresh_cookie(response, refresh_token)
    return build_session_response(user, refresh_token)


def _expired_cookie_headers() -> dict[str, str]:
    """The `Set-Cookie` that deletes the refresh cookie, as raise-able headers.

    Needed because FastAPI throws away the injected `Response` when a route
    RAISES: `HTTPException` is rendered by an exception handler that never sees
    it. Building the header off a throwaway `Response` keeps `core/cookies.py`
    the single source of truth for the cookie's attributes — and they must match
    exactly, or the browser treats the delete as a different cookie and keeps the
    original.
    """
    probe = Response()
    clear_refresh_cookie(probe)
    return {"set-cookie": probe.headers["set-cookie"]}


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
    response: Response,
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
    return issue_session(response, user, refresh)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh the session",
    description="Rotate a refresh token: the presented one is revoked, a new pair is issued.",
    responses={
        401: {"description": "No refresh token presented, or it is unknown/expired/reused."},
        403: {"description": "Cookie-authenticated without X-Brain-Client (CSRF guard)."},
        429: {"description": "Rate limited (per-IP auth budget)."},
    },
)
async def refresh(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Rotate-on-use: one refresh token yields exactly one successor. Reuse of an
    already-rotated token revokes the user's whole refresh family (theft signal).

    TWO WAYS IN, ONE PREFERRED. The `__Host-refresh_token` cookie wins whenever the
    browser sent one; the JSON body is the compatibility leg for a portal that has
    not migrated yet. Preferring the cookie matters during the migration itself: a
    client that presents both is one caught mid-deploy, and the cookie holds the
    leg this server rotated last.

    THE COOKIE IS THE ONLY AMBIENT CREDENTIAL IN THIS SERVICE, so it is the only
    one needing a CSRF guard — the browser attaches it to a cross-site form POST
    without the page asking. `X-Brain-Client` closes that: a form cannot set a
    request header, and a cross-site fetch() that tries is stopped by the CORS
    preflight. The body leg needs no such check (anyone able to put a valid refresh
    token in a request body already has the token).
    """
    _check_auth_rate_limit(request)
    from_cookie = read_refresh_cookie(request)
    if from_cookie is not None:
        require_client_header(request)
    presented = from_cookie or (payload.refresh_token if payload else None)
    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )
    result = await rotate_refresh_token(session, presented)
    if result is None:
        # A cookie that can never work again must not survive the rejection: left
        # in place it makes every future boot of that browser start with a doomed
        # /auth/refresh, and after a reuse-triggered family revocation it is
        # precisely the credential we just decided to distrust. Cleared ONLY when
        # the cookie is what failed — a legacy body-leg rejection says nothing
        # about a cookie that may belong to a live session in the same browser.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
            headers=_expired_cookie_headers() if from_cookie else None,
        )
    logger.info("token_refreshed", user_id=str(result.user.id))
    return issue_session(response, result.user, result.new_refresh_token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out",
    description="Revoke a refresh token. Always 204 (no token-existence oracle).",
)
async def logout(
    request: Request,
    payload: LogoutRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """End the revocable leg. The short-lived access token simply expires.

    Revokes BOTH legs when both are presented, so a client caught mid-migration
    cannot leave one of them alive, and always expires the cookie in the browser.

    NO `X-Brain-Client` CHECK HERE, unlike /auth/refresh — deliberately. The worst
    a forged logout achieves is signing the user out, while requiring the header
    would buy that back with a strictly worse failure: a logout refused with 403
    leaves the cookie in place after the portal has already dropped its in-memory
    session, so the very next reload silently signs the user back in. A route
    whose failure mode is "still logged in" must not be able to fail.
    """
    presented = {
        token
        for token in (
            read_refresh_cookie(request),
            payload.refresh_token if payload else None,
        )
        if token
    }
    for token in presented:
        await revoke_refresh_token(session, token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_refresh_cookie(response)
    return response


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
    response: Response,
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
    return issue_session(response, result.user, refresh)


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
    response: Response,
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
    return issue_session(response, user, refresh)


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


# --- Password reset (CONTRACTS.md §2.6) -------------------------------------
#
# The UNAUTHENTICATED recovery path, as opposed to /set-password above which requires a
# live session. Three steps mirroring PreCheck's contract exactly (request -> verify ->
# confirm) so brain-frontend's existing screens work against either backend unchanged.
#
# Before this existed, brain-frontend's "Esqueci a senha" called PreCheck's API — so for
# any user that exists only in brain-api (every self-serve signup) the reset silently did
# nothing: PreCheck found no such email and, correctly, returned its generic success.

# The ONE response the request endpoint ever gives. Deliberately worded so it is true
# whether or not the email matched — the user is told what will happen IF the account
# exists, never whether it does.
_RESET_REQUEST_MESSAGE = (
    "Se houver uma conta com esse e-mail, enviamos um link para redefinir a senha."
)
# Same message for unknown, expired and already-used tokens — distinguishing them would
# reveal whether a token ever existed.
_RESET_TOKEN_INVALID = "Token inválido ou expirado"


@router.post(
    "/password-reset/request",
    response_model=MessageOut,
    summary="Request a password reset link",
    description=(
        "Emails a single-use reset link. ALWAYS returns the same 200 body, whether or "
        "not the address belongs to an account, to prevent account enumeration."
    ),
    responses={429: {"description": "Rate limited (per-IP auth budget)."}},
)
async def password_reset_request(
    payload: PasswordResetRequestIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    """Begin the reset flow.

    ENUMERATION: there is exactly one `return` for both branches, so the status, body and
    headers are identical for a registered and an unregistered address. The unmatched
    branch does less WORK (no token write, no email dispatch), so the timing is not
    perfectly constant — matching PreCheck's own posture. Closing that side channel would
    need a dummy write plus a padded delay; it is recorded as a known limitation rather
    than papered over, because a half-done constant-time claim is worse than none.

    Shares the per-IP auth bucket with /token and /refresh on purpose: this route sends
    email to an address the caller chose, which is the more abusable of the two surfaces.
    """
    _check_auth_rate_limit(request)
    issued = await issue_password_reset_token(session, payload.email)
    if issued is not None:
        user, raw_token = issued
        settings = get_settings()
        # Same link mechanism as the professional invite (api/onboarding.py) — one
        # FRONTEND_BASE_URL for every emailed deep link. WHICH frontend that should be
        # now that secretarIA-frontend also serves these routes is an open deploy
        # decision, tracked with the invite link; do not fork a second setting for it.
        link = f"{settings.FRONTEND_BASE_URL}/esqueci_senha/token?token={raw_token}"
        # Fail-soft, like every other transactional email here: a bounced send must not
        # turn into a 500 that tells the caller this address exists.
        await secretaria_provisioning.send_notification_email(
            user.email,
            "password_reset",
            {
                "name": user.name,
                "link": link,
                "ttl_minutes": settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES,
            },
        )
        # Stable reference only — never the email or the token.
        logger.info("password_reset_requested", user_id=str(user.id))
    else:
        logger.info("password_reset_requested_no_match")
    return MessageOut(detail=_RESET_REQUEST_MESSAGE)


@router.post(
    "/password-reset/verify",
    response_model=MessageOut,
    summary="Check a reset token",
    description=(
        "Read-only pre-flight so the UI can reject a broken link before the user types "
        "a new password. Does NOT consume the token."
    ),
    responses={
        400: {"description": "Unknown, expired or already-used token."},
        429: {"description": "Rate limited (per-IP auth budget)."},
    },
)
async def password_reset_verify(
    payload: PasswordResetVerifyIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    """Validate without burning — the token must survive for the confirm step."""
    _check_auth_rate_limit(request)
    if await find_password_reset_user(session, payload.token) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _RESET_TOKEN_INVALID)
    return MessageOut(detail="Token válido")


@router.post(
    "/password-reset/confirm",
    response_model=MessageOut,
    summary="Set a new password with a reset token",
    description=(
        "Consumes the token and sets the new password. Afterwards the user logs in "
        "through POST /auth/token normally."
    ),
    responses={
        400: {"description": "Unknown, expired or already-used token."},
        422: {"description": "Password shorter than 8, longer than 72, or not letter+digit."},
        429: {"description": "Rate limited (per-IP auth budget)."},
    },
)
async def password_reset_confirm(
    payload: PasswordResetConfirmIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    """Consume the token and set the new password.

    NOT done here, deliberately: revoking the user's existing refresh tokens. It would be
    a defensible hardening (a reset is often triggered BY a compromise), but there is no
    revoke-all-for-user helper today and silently expanding the blast radius of this round
    into session invalidation is the kind of change that deserves its own review.
    """
    _check_auth_rate_limit(request)
    user = await _complete_password_reset(session, payload.token, payload.new_password)
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _RESET_TOKEN_INVALID)
    logger.info("password_reset_completed", user_id=str(user.id))
    return MessageOut(detail="Senha redefinida com sucesso")
