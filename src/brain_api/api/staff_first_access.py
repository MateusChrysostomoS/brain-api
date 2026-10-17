"""Only this surface accepts the staff_password_setup scope."""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.core.cookies import (
    STAFF_SETUP_COOKIE_NAME,
    clear_refresh_cookie,
    clear_staff_setup_cookie,
    require_client_header,
    set_staff_setup_cookie,
)
from brain_api.core.database import get_session
from brain_api.models import User
from brain_api.schemas.auth import SetPasswordIn, TokenResponse
from brain_api.services.staff_first_access import (
    complete_staff_first_access,
    create_staff_setup_token,
    resolve_staff_setup,
)

router = APIRouter(prefix="/auth/first-access", tags=["auth"])


def setup_response(response: Response, user: User, token: str | None = None) -> TokenResponse:
    from brain_api.core.security import decode_staff_token

    token = token or create_staff_setup_token(user)
    claims = decode_staff_token(token, "staff_password_setup", allow_legacy=True)
    expires = max(0, int(claims["exp"] - datetime.now(UTC).timestamp()))
    clear_refresh_cookie(response)
    set_staff_setup_cookie(response, token, expires)
    return TokenResponse(
        access_token=token,
        email=user.email,
        name=user.name,
        expires_in=expires,
        requires_password_change=True,
    )


class NoPendingFirstAccess(BaseModel):
    requires_password_change: Literal[False] = False


@router.post("/resume", response_model=TokenResponse | NoPendingFirstAccess)
async def resume(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
):
    require_client_header(request)
    token = request.cookies.get(STAFF_SETUP_COOKIE_NAME, "")
    if not token:
        # Older frontends already fall back to ordinary refresh for this JSON.
        # Do not issue a credential or clear the normal session's cookie.
        return NoPendingFirstAccess()
    try:
        user, _ = await resolve_staff_setup(session, token)
    except HTTPException:
        expired = Response()
        clear_staff_setup_cookie(expired)
        raise HTTPException(
            401, "Invalid or expired token", headers={"set-cookie": expired.headers["set-cookie"]}
        ) from None
    return setup_response(response, user, token)


@router.post("/complete", response_model=TokenResponse)
async def complete(
    payload: SetPasswordIn,
    response: Response,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    user, claims = await resolve_staff_setup(session, authorization[7:].strip())
    user = await complete_staff_first_access(
        session, user, claims["credential_version"], payload.new_password
    )
    from brain_api.api.auth import issue_session
    from brain_api.services.auth import issue_refresh_token

    refresh = await issue_refresh_token(session, user.id, expected_version=user.credential_version)
    clear_staff_setup_cookie(response)
    return issue_session(response, user, refresh)
