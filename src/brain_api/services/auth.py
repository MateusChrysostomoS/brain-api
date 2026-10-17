"""Auth service layer — credential verification, identity lookups, refresh tokens.

Mutable/sensitive state (the user row, the tenant) is read server-side here, never
trusted from the token. Decryption is not relevant in this scope (brain-api stores no
tenant secrets yet); `password_hash` is verified in-memory and never returned.

Refresh tokens (auth-jwt-multitenant): opaque values, stored HASHED (sha256), rotated
on every use. A presented token that was already rotated (revoked) is treated as reuse
— the whole active family for that user is revoked. Raw token values are never logged.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.core.security import (
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from brain_api.models import RefreshToken, StaffPasswordResetCode, Tenant, User

logger = get_logger(__name__)


async def authenticate(session: AsyncSession, email: str, password: str) -> User | None:
    """Return the user iff the email (case-insensitive) and password both match.

    Email is looked up lower-cased (users are stored lower-cased). An unknown email
    and a bad password yield the SAME `None` result — callers must not distinguish the
    two (CONTRACTS.md §2.1: one "Credenciais inválidas" message for both).
    """
    from brain_api.models import StaffAccount

    account = await session.scalar(
        select(StaffAccount).where(StaffAccount.email == email.strip().lower())
    )
    if account:
        if account.disabled_at or not verify_password(password, account.password_hash):
            return None
        proved_account_version = account.credential_version
        users = list(await session.scalars(select(User).where(User.account_id == account.id)))
        # Legacy login has no explicit selection contract. Never guess a clinic.
        if len(users) != 1:
            return None
        user = users[0]
        # Preserve the generation of the password just proved. A reset between that
        # proof and the membership read must not upgrade it to the new User version.
        account = await session.scalar(
            select(StaffAccount)
            .where(StaffAccount.id == account.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            not account
            or account.disabled_at
            or account.credential_version != proved_account_version
            or account.requires_password_change != user.requires_password_change
        ):
            return None
    else:
        user = await session.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None:
        return None
    if account is None and not verify_password(password, user.password_hash):
        return None
    if user.access_revoked_at is not None or user.membership_pending:
        return None
    if user.requires_password_change and (
        user.temporary_password_expires_at is None
        or _as_utc(user.temporary_password_expires_at) <= datetime.now(UTC)
    ):
        return None
    return user


async def get_user(session: AsyncSession, user_id: UUID) -> User | None:
    """Load a user by primary key (or None if it no longer exists)."""
    return await session.scalar(select(User).where(User.id == user_id))


async def get_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant | None:
    """Load a tenant by primary key (or None if it no longer exists)."""
    return await session.scalar(select(Tenant).where(Tenant.id == tenant_id))


async def exchange_invite_token(session: AsyncSession, raw_token: str) -> User | None:
    """Redeem a professional-invite token (`POST /doctor/professionals/invites`) — the
    professional-linkage sibling of `services.signup.exchange_onboarding_token`. Returns
    `None` (caller answers 401) for an unknown, expired, or already-burned token. On
    success burns the token (nulls the hash + expiry) in the SAME commit and returns the
    invited user, ready for the caller to mint a session; the user is then expected to
    call `POST /auth/set-password`.
    """
    user = await session.scalar(
        select(User)
        .where(User.invite_token_hash == hash_refresh_token(raw_token))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        user is None
        or user.invite_token_expires_at is None
        or _as_utc(user.invite_token_expires_at) <= datetime.now(UTC)
        or user.access_revoked_at is not None
        or user.membership_pending
    ):
        return None
    burned = await session.execute(
        update(User)
        .where(
            User.id == user.id,
            User.invite_token_hash == hash_refresh_token(raw_token),
            User.invite_token_expires_at > datetime.now(UTC),
            User.access_revoked_at.is_(None),
            User.membership_pending.is_(False),
        )
        .values(invite_token_hash=None, invite_token_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    if burned.rowcount != 1:
        await session.commit()
        return None
    proved_version = user.credential_version
    await session.commit()
    await session.refresh(user)
    if user.credential_version != proved_version:
        return None
    return user


async def set_password(
    session: AsyncSession, user_id: UUID, new_password: str, *, expected_version: int | None = None
) -> None:
    """Overwrite the CALLER'S OWN password hash (`POST /auth/set-password`).

    Needed because a signup-provisioned tenant owner (services/signup.py) starts on a
    random, never-communicated password. 401 if the user row no longer exists — the same
    failure mode `GET /auth/me` already handles for a valid token whose user was deleted.
    """
    user = await session.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        user is None
        or user.access_revoked_at
        or user.requires_password_change
        or user.membership_pending
        or expected_version is not None
        and user.credential_version != expected_version
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    if user.account_id:
        from brain_api.models import StaffAccount
        from brain_api.services.staff_account_recovery import change_password

        account_id = user.account_id
        # Association may have appeared while waiting for the contract migration.
        # Release the read lock, then use the credential lock order: account -> user.
        await session.commit()
        account = await session.scalar(
            select(StaffAccount)
            .where(StaffAccount.id == account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        user = await session.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            not user
            or user.account_id != account_id
            or user.access_revoked_at
            or user.requires_password_change
            or user.membership_pending
            or expected_version is not None
            and user.credential_version != expected_version
        ):
            raise HTTPException(401, "Invalid or expired token")
        await change_password(session, account, new_password)
        await session.commit()
        return
    user.password_hash = hash_password(new_password)
    user.invite_token_hash = None
    user.invite_token_expires_at = None
    await session.commit()


# --- Password reset ---------------------------------------------------------
#
# TASK-056: the person proves the mailbox with a 6-digit e-mailed code
# (`issue_password_reset_code` / `verify_password_reset_code`); a correct code is
# exchanged for the reset token — the SAME single-use, hashed-at-rest scheme as the
# professional invite (`exchange_invite_token` above): `secrets.token_urlsafe(32)` handed
# out once, only its sha256 persisted, burned by nulling the hash + expiry on redemption.
#
# NOTE ON ENUMERATION: none of these helpers raise or log differently for an unknown
# email/token — they return `None`, and it is the ROUTER's job to answer identically
# either way. Do not "improve" them by raising a specific error here.


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _hash_reset_code(code: str) -> str:
    """HMAC-SHA256 keyed by SECRET_KEY. A 6-digit code is reversible from a bare hash in
    a millisecond; keyed, a database leak alone reveals nothing. Rotating SECRET_KEY only
    invalidates the codes still in flight (10 minutes)."""
    return hmac.new(get_settings().SECRET_KEY.encode(), code.encode(), hashlib.sha256).hexdigest()


async def issue_password_reset_code(session: AsyncSession, email: str) -> tuple[User, str] | None:
    """Mint a 6-digit code for `email`, persisting only its keyed hash.

    `None` — and the router answers exactly as for a match — when no user has the
    address, when access is revoked, or when the per-user hourly cap is reached. Issuing
    overwrites the previous code (one live code per user) and zeroes `attempts`.
    """
    settings = get_settings()
    from brain_api.services import staff_account_recovery as recovery

    account = await recovery.find_account(session, email)
    if account:
        return await recovery.issue_code(session, account)
    user = await session.scalar(
        select(User)
        .where(User.email == _normalize_email(email))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user and user.account_id:
        await session.commit()
        account = await recovery.find_account(session, email)
        return await recovery.issue_code(session, account) if account else None
    if user is None or user.access_revoked_at is not None:
        logger.info("password_reset_code_requested_no_match")
        return None
    now = datetime.now(UTC)
    row = await session.get(StaffPasswordResetCode, user.id)
    if row is not None and _as_utc(row.window_started_at) > now - timedelta(hours=1):
        if row.issued_in_window >= settings.PASSWORD_RESET_CODE_MAX_PER_HOUR:
            logger.info("password_reset_code_throttled", user_id=str(user.id))
            return None
        window_started_at, issued = row.window_started_at, row.issued_in_window + 1
    else:
        window_started_at, issued = now, 1
    code = f"{secrets.randbelow(1_000_000):06d}"
    values = {
        "code_hash": _hash_reset_code(code),
        "expires_at": now + timedelta(minutes=settings.PASSWORD_RESET_CODE_EXPIRE_MINUTES),
        "attempts": 0,
        "window_started_at": window_started_at,
        "issued_in_window": issued,
    }
    if row is None:
        session.add(StaffPasswordResetCode(user_id=user.id, **values))
    else:
        for key, value in values.items():
            setattr(row, key, value)
    try:
        await session.commit()
    except IntegrityError:
        # Two first-ever requests raced on the primary key; the other one won and its
        # e-mail is on its way. Same answer to the caller either way.
        await session.rollback()
        return None
    return user, code


async def verify_password_reset_code(session: AsyncSession, email: str, code: str) -> str | None:
    """Exchange a correct code for a fresh single-use reset token; `None` otherwise.

    Same race-safe shape as `services/portal/patient_access.py::verify_account_otp`: the
    attempt is SPENT by a conditional UPDATE before comparing, so concurrent guesses get
    at most PASSWORD_RESET_CODE_MAX_ATTEMPTS comparisons between them, and naming the
    hash that was read refuses a code re-issued mid-request. A correct code is deleted
    and the reset token written in the same commit.
    """
    settings = get_settings()
    from brain_api.services import staff_account_recovery as recovery

    account = await recovery.find_account(session, email)
    if account:
        return await recovery.verify_code(session, account, code)
    ceiling = settings.PASSWORD_RESET_CODE_MAX_ATTEMPTS
    now = datetime.now(UTC)
    user = await session.scalar(
        select(User)
        .where(User.email == _normalize_email(email))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user and user.account_id:
        await session.commit()
        account = await recovery.find_account(session, email)
        return await recovery.verify_code(session, account, code) if account else None
    if user is None or user.access_revoked_at is not None:
        return None
    row = await session.get(StaffPasswordResetCode, user.id)
    if row is None or _as_utc(row.expires_at) <= now or row.attempts >= ceiling:
        return None
    # Read once, before the UPDATE below: `synchronize_session=False` leaves `row` stale.
    expected = row.code_hash
    spent = await session.execute(
        update(StaffPasswordResetCode)
        .where(
            StaffPasswordResetCode.user_id == user.id,
            StaffPasswordResetCode.code_hash == expected,
            StaffPasswordResetCode.attempts < ceiling,
        )
        .values(attempts=StaffPasswordResetCode.attempts + 1)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if spent.rowcount != 1 or not hmac.compare_digest(expected, _hash_reset_code(code)):
        return None
    # Reacquire the legacy identity before burning proof. The contract migration locks
    # identities before challenges; a request crossing adoption must retry via account.
    user = await session.scalar(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user.account_id:
        await session.commit()
        return None
    burned = await session.execute(
        delete(StaffPasswordResetCode).where(
            StaffPasswordResetCode.user_id == user.id,
            StaffPasswordResetCode.code_hash == expected,
        )
    )
    if burned.rowcount != 1:
        await session.rollback()
        return None
    raw_token = secrets.token_urlsafe(32)
    user.reset_token_hash = hash_refresh_token(raw_token)
    user.reset_token_expires_at = now + timedelta(
        minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    )
    await session.commit()
    logger.info("password_reset_code_verified", user_id=str(user.id))
    return raw_token


async def find_password_reset_user(session: AsyncSession, raw_token: str) -> User | None:
    """Resolve a reset token WITHOUT consuming it (the `/verify` pre-flight).

    `None` for unknown, expired, or already-burned — the caller must not distinguish
    those three, or the response becomes a probe for which tokens once existed.
    """
    user = await session.scalar(
        select(User)
        .where(User.reset_token_hash == hash_refresh_token(raw_token))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        user is None
        or user.reset_token_expires_at is None
        or _as_utc(user.reset_token_expires_at) <= datetime.now(UTC)
    ):
        return None
    return user


async def complete_password_reset(
    session: AsyncSession, raw_token: str, new_password: str
) -> User | None:
    """Consume the token and set the new password, in ONE commit.

    Re-validates rather than trusting a prior `/verify` call: the two are separate
    requests, and the token can expire (or be replaced by a newer reset request)
    between them. Returns `None` on an invalid/expired token.

    The password itself was already validated by `PasswordResetConfirmIn` (8-72 chars,
    letter + digit) — the same policy the account was created under.
    """
    from brain_api.services import staff_account_recovery as recovery

    account = await recovery.find_reset_account(session, raw_token)
    if account:
        return await recovery.complete_reset(session, account, raw_token, new_password)
    user = await find_password_reset_user(session, raw_token)
    if user is None or user.access_revoked_at is not None:
        return None
    if user.requires_password_change and verify_password(new_password, user.password_hash):
        raise HTTPException(422, "password_must_be_different")
    now = datetime.now(UTC)
    # Burn proof and old invitations atomically, including concurrent recovery requests.
    changed = await session.execute(
        update(User)
        .where(
            User.id == user.id,
            User.reset_token_hash == hash_refresh_token(raw_token),
            User.reset_token_expires_at > now,
            User.access_revoked_at.is_(None),
            User.account_id.is_(None),
        )
        .values(
            password_hash=hash_password(new_password),
            requires_password_change=False,
            temporary_password_expires_at=None,
            credential_version=User.credential_version + 1,
            reset_token_hash=None,
            reset_token_expires_at=None,
            invite_token_hash=None,
            invite_token_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        return None
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    await session.commit()
    await session.refresh(user)
    return user


# --- Refresh tokens ---------------------------------------------------------


def _as_utc(dt: datetime) -> datetime:
    """Normalize a DB datetime for comparison (SQLite returns naive; Postgres aware)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def issue_refresh_token(
    session: AsyncSession, user_id: UUID, *, expected_version: int | None = None
) -> str:
    """Create + persist a refresh token for the user; return the RAW value (once).

    Only the sha256 hash is stored. Commits (mirrors the write pattern of the other
    service layers — the request session is not auto-committing).
    """
    user = await session.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not user
        or user.access_revoked_at
        or user.requires_password_change
        or user.membership_pending
        or expected_version is not None
        and user.credential_version != expected_version
    ):
        raise HTTPException(401, "Invalid or expired token")
    raw = generate_refresh_token()
    session.add(
        RefreshToken(
            user_id=user_id,
            credential_version=user.credential_version,
            token_hash=hash_refresh_token(raw),
            expires_at=datetime.now(UTC) + timedelta(days=get_settings().REFRESH_TOKEN_EXPIRE_DAYS),
        )
    )
    await session.commit()
    return raw


@dataclass(frozen=True)
class RefreshResult:
    """A successful rotation: the (re)validated user + the NEW raw refresh token."""

    user: User
    new_refresh_token: str


async def rotate_refresh_token(session: AsyncSession, raw: str) -> RefreshResult | None:
    """Exchange a refresh token: revoke the presented one, issue a fresh one.

    Returns None (callers answer 401) when the token is unknown, expired, revoked, or
    its user no longer exists. A REVOKED token being presented again is the reuse/theft
    signal — every active token of that user is revoked before refusing, so a stolen
    rotated token can't be replayed and the legitimate session re-authenticates.
    """
    row = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw))
    )
    if row is None:
        return None
    # Consistent lock order with team revocation: membership, then refresh row.
    user = await session.scalar(
        select(User)
        .where(User.id == row.user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.id == row.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    now = datetime.now(UTC)
    if _as_utc(row.expires_at) <= now:
        return None

    if (
        user is None
        or user.access_revoked_at is not None
        or user.requires_password_change
        or user.membership_pending
        or row.credential_version is not None
        and row.credential_version != user.credential_version
        or user.account_id is not None
        and row.credential_version is None
    ):
        row.revoked_at = now
        await session.commit()
        return None
    if user.account_id:
        from brain_api.models import StaffAccount

        account = await session.get(StaffAccount, user.account_id, populate_existing=True)
        if not account or account.disabled_at or account.requires_password_change:
            row.revoked_at = now
            await session.commit()
            return None

    if row.revoked_at is not None:
        # A stale/expired proof is refused above. Reuse only affects its own current
        # generation; null legacy rows never revoke versioned successors.
        await session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.user_id == row.user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.credential_version == row.credential_version,
            )
            .values(revoked_at=now)
        )
        await session.commit()
        logger.warning("refresh_token_reuse_detected", user_id=str(row.user_id))
        return None

    # Rotate: kill the presented token, mint its successor (same commit).
    spent = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.id == row.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    if spent.rowcount != 1:
        await session.commit()
        return None
    new_raw = generate_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            credential_version=user.credential_version,
            token_hash=hash_refresh_token(new_raw),
            expires_at=now + timedelta(days=get_settings().REFRESH_TOKEN_EXPIRE_DAYS),
        )
    )
    await session.commit()
    return RefreshResult(user=user, new_refresh_token=new_raw)


async def revoke_refresh_token(session: AsyncSession, raw: str) -> None:
    """Logout: revoke the presented refresh token if it exists (idempotent, no oracle:
    callers return the same success response whether or not the token was known)."""
    row = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw))
    )
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await session.commit()
