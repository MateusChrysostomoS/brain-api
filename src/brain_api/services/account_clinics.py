"""Personal proof creates a fresh clinic; old memberships are never inputs."""

import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.models import SignupIntent, StaffAccount, StaffAccountSession, User
from brain_api.schemas.signup import AccountClinicCreate
from brain_api.services import signup, staff_accounts


async def lock_proof(
    session: AsyncSession, proof: tuple[StaffAccount, StaffAccountSession]
) -> tuple[StaffAccount, StaffAccountSession]:
    account, row = proof
    version = account.credential_version
    account = await session.scalar(
        select(StaffAccount)
        .where(StaffAccount.id == account.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    live, row = await staff_accounts.live_session(session, row.id)
    if live.credential_version != version:
        raise HTTPException(401, "Invalid or expired token")
    return live, row


async def find_operation(
    session: AsyncSession, account_id: UUID, operation_id: UUID
) -> signup.Registration:
    intent = await session.scalar(
        select(SignupIntent).where(
            SignupIntent.owner_account_id == account_id,
            SignupIntent.operation_id == operation_id,
        )
    )
    if intent is None:
        raise HTTPException(404, "signup_intent_not_found")
    user = await session.scalar(
        select(User).where(
            User.tenant_id == intent.tenant_id,
            User.account_id == account_id,
            User.is_owner.is_(True),
        )
    )
    if user is None:
        raise HTTPException(404, "signup_intent_not_found")
    # Creation does not bypass later revocation or altered membership authority.
    await staff_accounts.select_membership(
        session, await session.get(StaffAccount, account_id), user.id
    )
    return signup.Registration(intent=intent, user=user)


async def create_clinic(
    session: AsyncSession,
    proof: tuple[StaffAccount, StaffAccountSession],
    payload: AccountClinicCreate,
) -> signup.Registration:
    if not (
        get_settings().STAFF_ACCOUNT_CREDENTIALS_ENABLED
        and get_settings().STAFF_MULTI_CLINIC_ENABLED
    ):
        raise HTTPException(503, "staff_multi_clinic_not_enabled")
    account, _ = await lock_proof(session, proof)
    digest = hashlib.sha256(
        json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = await session.scalar(
        select(SignupIntent).where(
            SignupIntent.owner_account_id == account.id,
            SignupIntent.operation_id == payload.operation_id,
        )
    )
    if existing:
        if existing.request_hash != digest:
            raise HTTPException(409, "creation_operation_changed")
        return await find_operation(session, account.id, payload.operation_id)
    result = await signup._write_registration(
        session,
        payload,
        name=account.name,
        email=account.email,
        password_hash="!account-managed",
        account_id=account.id,
    )
    result.intent.owner_account_id = account.id
    result.intent.operation_id = payload.operation_id
    result.intent.request_hash = digest
    await session.commit()
    return result
