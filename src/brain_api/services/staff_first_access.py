"""Short-lived, purpose-restricted first access. No operational refresh credential."""

import secrets
import string
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from jose import jwt
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.security import (
    ALGORITHM,
    decode_staff_token,
    hash_password,
    staff_token_key,
    verify_password,
)
from brain_api.models import RefreshToken, User

SETUP_SCOPE = "staff_password_setup"


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def generate_temporary_password() -> str:
    return "A7" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(18))


def create_staff_setup_token(user: User) -> str:
    now = datetime.now(UTC)
    expiry = min(now + timedelta(minutes=10), aware(user.temporary_password_expires_at))
    return jwt.encode(
        {
            "sub": str(user.id),
            "tenant_id": str(user.tenant_id),
            "role": user.role,
            "scope": SETUP_SCOPE,
            "credential_version": user.credential_version,
            "iat": now,
            "exp": expiry,
        },
        staff_token_key(SETUP_SCOPE, get_settings().SECRET_KEY),
        algorithm=ALGORITHM,
    )


async def resolve_staff_setup(session: AsyncSession, token: str) -> tuple[User, dict]:
    claims = decode_staff_token(token, SETUP_SCOPE, allow_legacy=True)
    if not claims or claims.get("scope") != SETUP_SCOPE:
        raise HTTPException(401, "Invalid or expired token")
    try:
        uid, tid = UUID(claims["sub"]), UUID(claims["tenant_id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(401, "Invalid or expired token") from None
    user = await session.get(User, uid, populate_existing=True)
    version = claims.get("credential_version")
    if (
        user is None
        or user.tenant_id != tid
        or user.access_revoked_at is not None
        or not user.requires_password_change
        or user.temporary_password_expires_at is None
        or aware(user.temporary_password_expires_at) <= datetime.now(UTC)
        or type(version) is not int
        or version != user.credential_version
    ):
        raise HTTPException(401, "Invalid or expired token")
    return user, claims


async def complete_staff_first_access(
    session: AsyncSession, user: User, version: int, new_password: str
) -> User:
    if user.account_id:
        from sqlalchemy import select

        from brain_api.models import StaffAccount
        from brain_api.services.staff_account_recovery import change_password

        account = await session.scalar(
            select(StaffAccount)
            .where(StaffAccount.id == user.account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current = await session.scalar(
            select(User)
            .where(User.id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            not account
            or not account.requires_password_change
            or not account.temporary_password_expires_at
            or aware(account.temporary_password_expires_at) <= datetime.now(UTC)
            or current.access_revoked_at
            or not current.requires_password_change
            or current.credential_version != version
        ):
            raise HTTPException(401, "Invalid or expired token")
        await change_password(session, account, new_password)
        await session.commit()
        await session.refresh(user)
        if user.credential_version != version + 1:
            raise HTTPException(401, "Invalid or expired token")
        return user
    if verify_password(new_password, user.password_hash):
        raise HTTPException(422, "password_must_be_different")
    now = datetime.now(UTC)
    result = await session.execute(
        update(User)
        .where(
            User.id == user.id,
            User.credential_version == version,
            User.requires_password_change.is_(True),
            User.temporary_password_expires_at > now,
            User.access_revoked_at.is_(None),
            User.account_id.is_(None),
        )
        .values(
            password_hash=hash_password(new_password),
            requires_password_change=False,
            temporary_password_expires_at=None,
            credential_version=version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(401, "Invalid or expired token")
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await session.commit()
    await session.refresh(user)
    if user.credential_version != version + 1:
        raise HTTPException(401, "Invalid or expired token")
    return user
