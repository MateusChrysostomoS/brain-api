"""Account recovery: preserve quotas and never accept pending clinic invitations."""

import hmac
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.security import hash_password, hash_refresh_token, verify_password
from brain_api.models import (
    RefreshToken,
    StaffAccount,
    StaffAccountResetCode,
    StaffAccountSession,
    User,
)
from brain_api.services.staff_accounts import aware, normalized_email


async def find_account(session: AsyncSession, email: str) -> StaffAccount | None:
    return await session.scalar(
        select(StaffAccount)
        .where(StaffAccount.email == normalized_email(email))
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def issue_code(session: AsyncSession, account: StaffAccount):
    from brain_api.services.auth import _hash_reset_code

    if account.disabled_at:
        return None
    now, settings = datetime.now(UTC), get_settings()
    row = await session.get(StaffAccountResetCode, account.id, populate_existing=True)
    if row and aware(row.window_started_at) > now - timedelta(hours=1):
        if row.issued_in_window >= settings.PASSWORD_RESET_CODE_MAX_PER_HOUR:
            return None
        window, count = row.window_started_at, row.issued_in_window + 1
    else:
        window, count = now, 1
    code = f"{secrets.randbelow(1_000_000):06d}"
    values = dict(
        code_hash=_hash_reset_code(code),
        expires_at=now + timedelta(minutes=settings.PASSWORD_RESET_CODE_EXPIRE_MINUTES),
        attempts=0,
        window_started_at=window,
        issued_in_window=count,
        consumed_at=None,
    )
    if not row:
        session.add(StaffAccountResetCode(account_id=account.id, **values))
    else:
        for key, value in values.items():
            setattr(row, key, value)
    await session.commit()
    return account, code


async def verify_code(session: AsyncSession, account: StaffAccount, code: str):
    from brain_api.services.auth import _hash_reset_code

    if account.disabled_at:
        return None
    now, settings = datetime.now(UTC), get_settings()
    row = await session.get(StaffAccountResetCode, account.id, populate_existing=True)
    if not row or row.consumed_at or aware(row.expires_at) <= now:
        return None
    expected = row.code_hash
    spent = await session.execute(
        update(StaffAccountResetCode)
        .where(
            StaffAccountResetCode.account_id == account.id,
            StaffAccountResetCode.code_hash == expected,
            StaffAccountResetCode.consumed_at.is_(None),
            StaffAccountResetCode.expires_at > now,
            StaffAccountResetCode.attempts < settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS,
        )
        .values(attempts=StaffAccountResetCode.attempts + 1)
        .execution_options(synchronize_session=False)
    )
    if spent.rowcount != 1 or not hmac.compare_digest(expected, _hash_reset_code(code)):
        await session.commit()
        return None
    burned = await session.execute(
        update(StaffAccountResetCode)
        .where(
            StaffAccountResetCode.account_id == account.id,
            StaffAccountResetCode.code_hash == expected,
            StaffAccountResetCode.consumed_at.is_(None),
        )
        .values(consumed_at=now)
        .execution_options(synchronize_session=False)
    )
    if burned.rowcount != 1:
        await session.commit()
        return None
    raw = secrets.token_urlsafe(32)
    account.reset_token_hash = hash_refresh_token(raw)
    account.reset_token_expires_at = now + timedelta(
        minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    )
    await session.commit()
    return raw


async def find_reset_account(session: AsyncSession, token: str):
    return await session.scalar(
        select(StaffAccount)
        .where(StaffAccount.reset_token_hash == hash_refresh_token(token))
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def change_password(session: AsyncSession, account: StaffAccount, password: str):
    """Caller holds the account lock; invalidate all credential/session generations."""
    if account.disabled_at:
        raise HTTPException(401, "Invalid or expired token")
    if account.requires_password_change and verify_password(password, account.password_hash):
        raise HTTPException(422, "password_must_be_different")
    now = datetime.now(UTC)
    account.password_hash = hash_password(password)
    account.credential_version += 1
    account.requires_password_change = False
    account.temporary_password_expires_at = None
    account.reset_token_hash = None
    account.reset_token_expires_at = None
    await session.execute(
        update(StaffAccountResetCode)
        .where(StaffAccountResetCode.account_id == account.id)
        .values(consumed_at=now)
    )
    # Legacy hashes cease being usable, without copying the new credential per clinic.
    # Non-secret sentinel refuses bcrypt verification; auth always uses the account.
    await session.execute(
        update(User)
        .where(User.account_id == account.id)
        .values(
            password_hash="!account-managed",
            credential_version=case(
                (User.membership_pending.is_(True), User.credential_version),
                else_=User.credential_version + 1,
            ),
            requires_password_change=False,
            temporary_password_expires_at=None,
            reset_token_hash=None,
            reset_token_expires_at=None,
            invite_token_hash=None,
            invite_token_expires_at=None,
        )
    )
    ids = select(User.id).where(User.account_id == account.id)
    await session.execute(
        update(RefreshToken).where(RefreshToken.user_id.in_(ids)).values(revoked_at=now)
    )
    await session.execute(
        update(StaffAccountSession)
        .where(StaffAccountSession.account_id == account.id)
        .values(revoked_at=now)
    )


async def complete_reset(session: AsyncSession, account: StaffAccount, token: str, password: str):
    now = datetime.now(UTC)
    if (
        account.disabled_at
        or not account.reset_token_expires_at
        or aware(account.reset_token_expires_at) <= now
        or account.reset_token_hash != hash_refresh_token(token)
    ):
        return None
    if account.requires_password_change and verify_password(password, account.password_hash):
        raise HTTPException(422, "password_must_be_different")
    spent = await session.execute(
        update(StaffAccount)
        .where(
            StaffAccount.id == account.id,
            StaffAccount.reset_token_hash == hash_refresh_token(token),
            StaffAccount.reset_token_expires_at > now,
            StaffAccount.disabled_at.is_(None),
        )
        .values(reset_token_hash=None, reset_token_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    if spent.rowcount != 1:
        await session.commit()
        return None
    await change_password(session, account, password)
    await session.commit()
    return account
