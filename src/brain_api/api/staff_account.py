"""Personal staff sessions open no clinic without explicit authorized selection."""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.cookies import (
    STAFF_ACCOUNT_COOKIE_NAME,
    clear_refresh_cookie,
    clear_staff_account_cookie,
    clear_staff_setup_cookie,
    require_client_header,
    set_staff_account_cookie,
)
from brain_api.core.database import get_session
from brain_api.models import StaffAccountSession
from brain_api.schemas.auth import LoginRequest, SetPasswordIn, TokenResponse
from brain_api.schemas.signup import AccountClinicCreate, AccountClinicOut, CheckoutSessionOut
from brain_api.schemas.staff_account import (
    AcceptInviteIn,
    AccountResponse,
    MembershipOut,
    PreferenceIn,
    SelectMembershipIn,
)
from brain_api.services import staff_accounts as service

router = APIRouter(prefix="/auth/account", tags=["auth"])
COOKIE = STAFF_ACCOUNT_COOKIE_NAME


def clinic_body(registration, row):
    from brain_api.api.auth import build_session_response
    from brain_api.schemas.signup import AccountClinicOut

    intent = registration.intent
    return AccountClinicOut(
        intent_id=intent.id, operation_id=intent.operation_id,
        clinic_name=intent.clinic_name, whatsapp_phone=intent.whatsapp_phone,
        catalog_ids=intent.catalog_ids, status=intent.status,
        session=build_session_response(registration.user, None, account_session_id=str(row.id)),
    )


def set_cookie(response: Response, raw: str):
    set_staff_account_cookie(response, raw)


def clear_cookie(response: Response):
    clear_staff_account_cookie(response)


async def current_account(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    return await service.resolve_account(session, authorization[7:].strip())


@router.post("/clinics", status_code=201, response_model=AccountClinicOut)
async def create_clinic(
    payload: AccountClinicCreate, response: Response, proof=Depends(current_account),
    session: AsyncSession = Depends(get_session),
):
    from brain_api.services import account_clinics

    response.headers["Cache-Control"] = "no-store"
    result = await account_clinics.create_clinic(session, proof, payload)
    return clinic_body(result, proof[1])


@router.get("/clinics/{operation_id}", response_model=AccountClinicOut)
async def resume_clinic(
    operation_id: UUID, response: Response, proof=Depends(current_account),
    session: AsyncSession = Depends(get_session),
):
    from brain_api.services import account_clinics

    response.headers["Cache-Control"] = "no-store"
    account, row = await account_clinics.lock_proof(session, proof)
    result = await account_clinics.find_operation(session, account.id, operation_id)
    return clinic_body(result, row)


@router.post("/clinics/{operation_id}/checkout", response_model=CheckoutSessionOut)
async def clinic_checkout(
    operation_id: UUID, response: Response, proof=Depends(current_account),
    session: AsyncSession = Depends(get_session),
):
    from brain_api.services import account_clinics, signup

    response.headers["Cache-Control"] = "no-store"
    account, _ = await account_clinics.lock_proof(session, proof)
    result = await account_clinics.find_operation(session, account.id, operation_id)
    url = await signup.create_checkout_session_for_intent(
        session, result.intent.id, owner_account_id=account.id,
    )
    return CheckoutSessionOut(checkout_url=url)


async def response_body(session, account, row):
    memberships = await service.list_memberships(session, account)
    preferred = next((m for m in memberships if m.user_id == account.preferred_user_id), None)
    state = (
        "selection_required"
        if not account.preferred_user_id
        else (
            "preferred_active"
            if preferred and preferred.status == "active"
            else "preferred_unavailable"
        )
    )
    clinical = None
    if state == "preferred_active":
        try:
            user = await service.select_membership(session, account, account.preferred_user_id)
        except HTTPException as exc:
            if exc.status_code not in (403, 404, 409):
                raise
            state = "preferred_unavailable"
            memberships = await service.list_memberships(session, account)
        else:
            from brain_api.api.auth import build_session_response

            clinical = build_session_response(user, None, account_session_id=str(row.id))
    return AccountResponse(
        account_token=service.account_token(account, row),
        expires_in=get_settings().ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        email=account.email,
        name=account.name,
        memberships=memberships,
        preferred_user_id=account.preferred_user_id,
        selection_state=state,
        clinical_session=clinical,
    )


@router.post("/login", response_model=AccountResponse | TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    if not get_settings().STAFF_ACCOUNT_CREDENTIALS_ENABLED:
        raise HTTPException(503, "staff_account_not_enabled")
    from brain_api.api.auth import _check_auth_rate_limit

    _check_auth_rate_limit(request)
    account = await service.authenticate_account(session, payload.email, payload.password)
    if account is None:
        raise HTTPException(401, "Credenciais inválidas")
    if account.requires_password_change:
        from brain_api.api.staff_first_access import setup_response
        from brain_api.models import User

        user = await session.get(User, account.id)
        if not user or user.access_revoked_at:
            raise HTTPException(401, "Credenciais inválidas")
        return setup_response(response, user)
    row, raw = await service.open_account_session(session, account)
    clear_refresh_cookie(response)
    clear_staff_setup_cookie(response)
    set_cookie(response, raw)
    response.headers["Cache-Control"] = "no-store"
    return await response_body(session, account, row)


@router.get("/memberships", response_model=list[MembershipOut])
async def memberships(
    response: Response, proof=Depends(current_account), session: AsyncSession = Depends(get_session)
):
    response.headers["Cache-Control"] = "no-store"
    return await service.list_memberships(session, proof[0])


@router.post("/first-access/complete", response_model=AccountResponse)
async def complete_first_access(
    payload: SetPasswordIn,
    response: Response,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    if not get_settings().STAFF_ACCOUNT_CREDENTIALS_ENABLED:
        raise HTTPException(503, "staff_account_not_enabled")
    from brain_api.models import StaffAccount
    from brain_api.services.staff_first_access import (
        complete_staff_first_access,
        resolve_staff_setup,
    )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    user, claims = await resolve_staff_setup(session, authorization[7:].strip())
    account = await session.get(StaffAccount, user.account_id) if user.account_id else None
    # Freeze the resulting personal generation BEFORE completing/releasing proof.
    expected_account_version = account.credential_version + 1 if account else None
    user = await complete_staff_first_access(
        session, user, claims["credential_version"], payload.new_password
    )
    proved_membership_version = user.credential_version
    account = await session.get(StaffAccount, user.account_id)
    if not account:
        account = await service.adopt_user(
            session, user, expected_version=proved_membership_version
        )
        expected_account_version = account.credential_version
        await session.commit()
    row, raw = await service.open_account_session(
        session, account, expected_version=expected_account_version
    )
    clear_refresh_cookie(response)
    clear_staff_setup_cookie(response)
    set_cookie(response, raw)
    response.headers["Cache-Control"] = "no-store"
    return await response_body(session, account, row)


@router.post("/select", response_model=TokenResponse)
async def select(
    payload: SelectMembershipIn,
    response: Response,
    proof=Depends(current_account),
    session: AsyncSession = Depends(get_session),
):
    response.headers["Cache-Control"] = "no-store"
    account, row = proof
    if payload.remember:
        await service.remember_membership(session, account, payload.user_id)
    user = await service.select_membership(session, account, payload.user_id)
    from brain_api.api.auth import build_session_response

    return build_session_response(user, None, account_session_id=str(row.id))


@router.put("/preference", status_code=204)
async def preference(
    payload: PreferenceIn,
    proof=Depends(current_account),
    session: AsyncSession = Depends(get_session),
):
    await service.remember_membership(session, proof[0], payload.user_id)
    return Response(status_code=204)


@router.post("/invites/accept", response_model=list[MembershipOut])
async def accept_invite(
    payload: AcceptInviteIn,
    response: Response,
    proof=Depends(current_account),
    session: AsyncSession = Depends(get_session),
):
    response.headers["Cache-Control"] = "no-store"
    await service.accept_invite(session, proof[0], payload.token, session_id=proof[1].id)
    return await service.list_memberships(session, proof[0])


@router.post("/resume", response_model=AccountResponse)
async def resume(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
):
    require_client_header(request)
    from brain_api.api.auth import _check_auth_rate_limit

    _check_auth_rate_limit(request)
    try:
        account, row, raw = await service.rotate_account_session(
            session, request.cookies.get(COOKIE, "")
        )
    except HTTPException:
        expired = Response()
        clear_cookie(expired)
        raise HTTPException(
            401, "Invalid or expired token", headers={"set-cookie": expired.headers["set-cookie"]}
        ) from None
    set_cookie(response, raw)
    response.headers["Cache-Control"] = "no-store"
    return await response_body(session, account, row)


@router.post("/logout", status_code=204)
async def logout(request: Request, session: AsyncSession = Depends(get_session)):
    from sqlalchemy import select

    from brain_api.core.security import hash_refresh_token

    row = await session.scalar(
        select(StaffAccountSession).where(
            StaffAccountSession.token_hash == hash_refresh_token(request.cookies.get(COOKIE, ""))
        )
    )
    authorization = request.headers.get("authorization", "")
    bearer_account = None
    if authorization.lower().startswith("bearer "):
        try:
            bearer_account, bearer_row = await service.resolve_account(
                session, authorization[7:].strip()
            )
        except HTTPException:
            from uuid import UUID

            from brain_api.core.security import decode_token
            from brain_api.models import User

            claims = decode_token(authorization[7:].strip())
            if claims and not claims.get("scope") and claims.get("account_sid"):
                try:
                    candidate, candidate_row = await service.live_session(
                        session, UUID(claims["account_sid"])
                    )
                    user = await session.get(User, UUID(claims["sub"]))
                    if user and user.account_id == candidate.id:
                        bearer_account = candidate
                        bearer_row = candidate_row
                except (HTTPException, ValueError, TypeError, KeyError):
                    pass
    if bearer_account:
        await service.end_account_sessions(
            session, bearer_account.id, expected_version=bearer_row.credential_version
        )
    if row and (not bearer_account or row.account_id != bearer_account.id):
        await service.end_cookie_account(session, request.cookies.get(COOKIE, ""))
    await session.commit()
    response = Response(status_code=204)
    clear_cookie(response)
    clear_refresh_cookie(response)
    clear_staff_setup_cookie(response)
    return response
