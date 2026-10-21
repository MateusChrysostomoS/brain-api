"""Account proof and explicit clinical memberships. Never discover by shared email."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from jose import jwt
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.core.security import (
    ALGORITHM,
    decode_staff_token,
    generate_refresh_token,
    hash_refresh_token,
    staff_token_key,
    verify_password,
)
from brain_api.models import (
    StaffAccount,
    StaffAccountResetCode,
    StaffAccountSession,
    StaffPasswordResetCode,
    Tenant,
    User,
)
from brain_api.schemas.staff_account import MembershipOut

logger = get_logger(__name__)


def normalized_email(email: str) -> str:
    return email.strip().lower()


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def email_registered(session: AsyncSession, email: str) -> bool:
    value = normalized_email(email)
    return bool(
        await session.scalar(select(StaffAccount.id).where(StaffAccount.email == value))
        or await session.scalar(select(User.id).where(User.email == value))
    )


async def adopt_new_user(session: AsyncSession, user: User) -> StaffAccount | None:
    # Expansion remains writable by the old release until every writer is account-aware.
    if not get_settings().STAFF_ACCOUNT_CREDENTIALS_ENABLED:
        return None
    return await adopt_user(session, user)


async def adopt_user(
    session: AsyncSession, user: User, *, expected_version: int | None = None
) -> StaffAccount | None:
    """Adopt exactly one legacy identity. Never join an existing account by email."""
    if user.role == "admin" or user.tenant_id is None:
        return None
    if user.account_id and expected_version is None:
        return await session.get(StaffAccount, user.account_id)
    # The lock serializes adoption and credential changes on real PostgreSQL.
    user = await session.scalar(
        select(User)
        .where(User.id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if expected_version is not None and user.credential_version != expected_version:
        raise HTTPException(401, "Invalid or expired token")
    if user.account_id:
        return await session.get(StaffAccount, user.account_id)
    existing = await session.scalar(
        select(StaffAccount.id).where(StaffAccount.email == normalized_email(user.email))
    )
    if existing:
        # A colliding legacy writer must never turn an attribute match into a grant.
        return None
    account = StaffAccount(
        id=user.id,
        email=normalized_email(user.email),
        name=user.name,
        password_hash=user.password_hash,
        credential_version=0,
        requires_password_change=user.requires_password_change,
        temporary_password_expires_at=user.temporary_password_expires_at,
        reset_token_hash=user.reset_token_hash,
        reset_token_expires_at=user.reset_token_expires_at,
    )
    session.add(account)
    await session.flush()
    user.account_id = account.id
    old_code = await session.get(StaffPasswordResetCode, user.id)
    if old_code:
        session.add(
            StaffAccountResetCode(
                account_id=account.id,
                code_hash=old_code.code_hash,
                expires_at=old_code.expires_at,
                attempts=old_code.attempts,
                window_started_at=old_code.window_started_at,
                issued_in_window=old_code.issued_in_window,
            )
        )
        await session.delete(old_code)
    user.reset_token_hash = None
    user.reset_token_expires_at = None
    await session.flush()
    return account


async def authenticate_account(
    session: AsyncSession, email: str, password: str
) -> StaffAccount | None:
    account = await session.scalar(
        select(StaffAccount).where(StaffAccount.email == normalized_email(email))
    )
    if account is None:
        users = list(
            await session.scalars(select(User).where(User.email == normalized_email(email)))
        )
        if len(users) != 1 or not verify_password(password, users[0].password_hash):
            return None
        account = await adopt_user(session, users[0])
    if (
        account is None
        or account.disabled_at
        or not verify_password(password, account.password_hash)
    ):
        return None
    if account.requires_password_change and (
        not account.temporary_password_expires_at
        or aware(account.temporary_password_expires_at) <= datetime.now(UTC)
    ):
        return None
    await session.commit()
    return account


async def list_memberships(session: AsyncSession, account: StaffAccount) -> list[MembershipOut]:
    rows = await session.execute(
        select(User, Tenant)
        .join(Tenant, Tenant.id == User.tenant_id)
        .where(User.account_id == account.id)
        .order_by(User.created_at, User.id)
    )
    result = [
        MembershipOut(
            user_id=user.id,
            tenant_id=tenant.id,
            clinic_name=tenant.clinic_name,
            status=(
                "revoked"
                if user.access_revoked_at
                else "pending_invite"
                if user.membership_pending
                else "pending_first_access"
                if user.requires_password_change
                else "active"
            ),
        )
        for user, tenant in rows
    ]
    return result


ACCOUNT_SCOPE = "staff_account"


def _account_signing_key(secret: str) -> str:
    if not secret:
        raise HTTPException(503, "staff_auth_not_configured")
    return staff_token_key(ACCOUNT_SCOPE, secret)


def decode_account_token(token: str) -> dict | None:
    return decode_staff_token(token, ACCOUNT_SCOPE)


async def open_account_session(
    session: AsyncSession, account: StaffAccount, *, expected_version: int | None = None
):
    proved_version = account.credential_version if expected_version is None else expected_version
    account = await session.scalar(
        select(StaffAccount)
        .where(StaffAccount.id == account.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not account
        or account.disabled_at
        or account.requires_password_change
        or account.credential_version != proved_version
    ):
        raise HTTPException(401, "Invalid or expired token")
    secret = generate_refresh_token()
    row = StaffAccountSession(
        id=uuid4(),
        account_id=account.id,
        token_hash=hash_refresh_token(secret),
        credential_version=account.credential_version,
        expires_at=datetime.now(UTC) + timedelta(days=get_settings().REFRESH_TOKEN_EXPIRE_DAYS),
    )
    session.add(row)
    await session.commit()
    return row, cookie_value(row.id, secret)


def cookie_value(session_id: UUID, secret: str) -> str:
    """`<row id>.<secret>`: the cookie names its row, so ANY stale secret for a live row is
    attributable and reads as reuse (TASK-065 review H1). Only the secret's hash is stored."""
    return f"{session_id}.{secret}"


def parse_cookie_value(raw: str) -> tuple[UUID | None, str] | None:
    """`(row id, secret)`; `(None, raw)` for a legacy value (pre-TASK-065, no row id, found by
    hash until its first rotation); `None` for a malformed value, which names no row.

    `generate_refresh_token` is URL-safe base64 (never a dot), so a dot means the new format.
    """
    if not raw:
        return None
    if "." not in raw:
        return None, raw
    head, _, secret = raw.partition(".")
    if not secret or "." in secret:
        return None
    try:
        return UUID(head), secret
    except ValueError:
        return None


def account_token(account: StaffAccount, row: StaffAccountSession) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(account.id),
            "sid": str(row.id),
            "scope": ACCOUNT_SCOPE,
            "credential_version": row.credential_version,
            "iat": now,
            "exp": now + timedelta(minutes=get_settings().ACCESS_TOKEN_EXPIRE_MINUTES),
        },
        _account_signing_key(get_settings().SECRET_KEY),
        algorithm=ALGORITHM,
    )


async def live_session(
    session: AsyncSession, sid: UUID
) -> tuple[StaffAccount, StaffAccountSession]:
    row = await session.get(StaffAccountSession, sid, populate_existing=True)
    account = (
        await session.get(StaffAccount, row.account_id, populate_existing=True) if row else None
    )
    if (
        not row
        or row.revoked_at
        or aware(row.expires_at) <= datetime.now(UTC)
        or not account
        or account.disabled_at
        or account.requires_password_change
        or account.credential_version != row.credential_version
    ):
        raise HTTPException(401, "Invalid or expired token")
    return account, row


async def resolve_account(session: AsyncSession, token: str):
    claims = decode_account_token(token)
    if not claims or claims.get("scope") != ACCOUNT_SCOPE:
        raise HTTPException(401, "Invalid or expired token")
    try:
        aid, sid = UUID(claims["sub"]), UUID(claims["sid"])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(401, "Invalid or expired token") from None
    account, row = await live_session(session, sid)
    if (
        aid != account.id
        or type(claims.get("credential_version")) is not int
        or claims.get("credential_version") != account.credential_version
    ):
        raise HTTPException(401, "Invalid or expired token")
    return account, row


async def select_membership(session: AsyncSession, account: StaffAccount, user_id: UUID) -> User:
    # Same row lock as revocation/management. A response racing with revocation cannot
    # operate afterwards: every consumer checks live membership/version.
    user = await session.scalar(
        select(User)
        .where(
            User.id == user_id,
            User.account_id == account.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise HTTPException(404, "membership_not_found")
    if user.access_revoked_at:
        raise HTTPException(403, "membership_revoked")
    if user.requires_password_change or user.invite_token_hash or user.membership_pending:
        raise HTTPException(409, "membership_pending")
    if user.tenant_id is None or user.role == "admin":
        raise HTTPException(403, "membership_not_available")
    return user


async def remember_membership(session: AsyncSession, account: StaffAccount, user_id: UUID | None):
    proved_version = account.credential_version
    account = await session.scalar(
        select(StaffAccount)
        .where(StaffAccount.id == account.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not account
        or account.disabled_at
        or account.requires_password_change
        or account.credential_version != proved_version
    ):
        raise HTTPException(401, "Invalid or expired token")
    if user_id is not None:
        await select_membership(session, account, user_id)
    account.preferred_user_id = user_id
    await session.commit()


def _cookie_value_matches(presented: str):
    """A cookie names its row by the CURRENT value or the one the last resume replaced."""
    return or_(
        StaffAccountSession.token_hash == presented,
        StaffAccountSession.previous_token_hash == presented,
    )


async def _cookie_row(
    session: AsyncSession, raw: str
) -> tuple[StaffAccountSession | None, str | None, bool]:
    """`(row, presented secret hash, names_row)` for a cookie value.

    New format: the row its id names (whatever the secret is — the caller decides whether the
    secret is current, previous or stale). Legacy format (no id): only a row whose current or
    previous hash matches. Malformed: nothing. `names_row` is True only for the new format,
    where a non-matching secret is attributable to the row (reuse signal).
    """
    parsed = parse_cookie_value(raw)
    if parsed is None:
        return None, None, False
    session_id, secret = parsed
    presented = hash_refresh_token(secret)
    if session_id is None:
        row = await session.scalar(
            select(StaffAccountSession)
            .where(_cookie_value_matches(presented))
            .execution_options(populate_existing=True)
        )
        return row, presented, False
    row = await session.scalar(
        select(StaffAccountSession)
        .where(StaffAccountSession.id == session_id)
        .execution_options(populate_existing=True)
    )
    return row, presented, True


async def _reuse_detected(session: AsyncSession, row: StaffAccountSession):
    await end_account_sessions(session, row.account_id)
    await session.commit()
    # The session id only — never a cookie value or hash, never the address or account id.
    logger.warning("staff_account_session_reuse_detected", session_id=str(row.id))
    raise HTTPException(401, "Invalid or expired token")


async def rotate_account_session(
    session: AsyncSession, raw: str
) -> tuple[StaffAccount, StaffAccountSession, str | None]:
    """Renew the personal sign-in a cookie names — IN PLACE — or raise 401 (TASK-065).

    The row keeps its `id` (the account token's `sid` and every clinical token's
    `account_sid` name it, across every tab of the person); what rotates is the SECRET: a
    fresh one replaces `token_hash`, the old hash moves to `previous_token_hash` with
    `rotated_at`, and `expires_at` slides to now + REFRESH_TOKEN_EXPIRE_DAYS (the same sliding
    lifetime the old new-row-per-resume gave). The cookie is `<row id>.<secret>`, so a
    presented value is always attributable to its row. Outcomes:

    1. the CURRENT secret of a live row → rotate by compare-and-swap (`UPDATE ... WHERE
       token_hash = <presented>`, only rowcount 1 counts) and return the new cookie value;
    2. the PREVIOUS secret, rotated less than STAFF_ACCOUNT_SESSION_ROTATION_GRACE_SECONDS ago
       (a renewal already in flight, another tab, or the loser of the swap) → accept without
       rotating again; the third element is `None` and the caller sets NO cookie;
    3. ANY OTHER secret for a row of the live generation — the previous one after the window,
       one from two or more rotations back, garbage — and the current secret of a revoked row
       → reuse/theft signal: every session and clinical token of the account ends
       (`end_account_sessions`) and 401. A thief who rotates twice cannot shake this off.
    Plain 401 (nothing ends): malformed value, unknown row, expired row, older generation.
    A legacy cookie (no row id, issued before TASK-065) is found by hash — current or
    previous — and leaves its first successful rotation in the new format.
    """
    settings = get_settings()
    row, presented, names_row = await _cookie_row(session, raw)
    if not row:
        raise HTTPException(401, "Invalid or expired token")
    # The account lock serializes this with credential changes and with a concurrent resume
    # of the same account on PostgreSQL; the swap below is what decides on any database.
    account = await session.scalar(
        select(StaffAccount)
        .where(StaffAccount.id == row.account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = await session.scalar(
        select(StaffAccountSession)
        .where(StaffAccountSession.id == row.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = datetime.now(UTC)
    if (
        not account
        or aware(row.expires_at) <= now
        or row.credential_version != account.credential_version
    ):
        raise HTTPException(401, "Invalid or expired token")
    if row.token_hash == presented:
        if row.revoked_at:
            await _reuse_detected(session, row)
        account, row = await live_session(session, row.id)
        secret = generate_refresh_token()
        swapped = await session.execute(
            update(StaffAccountSession)
            .where(
                StaffAccountSession.id == row.id,
                StaffAccountSession.token_hash == presented,
                StaffAccountSession.revoked_at.is_(None),
            )
            .values(
                token_hash=hash_refresh_token(secret),
                previous_token_hash=presented,
                rotated_at=now,
                expires_at=now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            )
            .execution_options(synchronize_session=False)
        )
        if swapped.rowcount == 1:
            await session.commit()
            await session.refresh(row)
            return account, row, cookie_value(row.id, secret)
        # Lost the race to another resume of this same value. No rollback on purpose: the
        # re-read below must see the winner's write (and under a shared connection a
        # rollback would discard it). Decide grace-vs-theft on FRESH attributes only.
        row = await session.scalar(
            select(StaffAccountSession)
            .where(StaffAccountSession.id == row.id)
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise HTTPException(401, "Invalid or expired token")
        names_row = True  # it was this row's current secret a moment ago
    grace = timedelta(seconds=settings.STAFF_ACCOUNT_SESSION_ROTATION_GRACE_SECONDS)
    if (
        row.previous_token_hash == presented
        and row.revoked_at is None
        and row.rotated_at is not None
        and aware(row.rotated_at) + grace > now
    ):
        account, row = await live_session(session, row.id)
        await session.commit()  # nothing to write; releases the row/account locks
        logger.info("staff_account_session_grace_accepted", session_id=str(row.id))
        return account, row, None
    if names_row or row.previous_token_hash == presented:
        await _reuse_detected(session, row)
    raise HTTPException(401, "Invalid or expired token")


async def accept_invite(
    session: AsyncSession, account: StaffAccount, token: str, *, session_id=None
):
    from brain_api.services.staff_invitations import accept_invitation

    return await accept_invitation(session, account, token, session_id=session_id)


async def end_account_sessions(
    session: AsyncSession, account_id: UUID, *, expected_version: int | None = None
):
    from brain_api.models import RefreshToken

    now = datetime.now(UTC)
    # A concurrent refresh's newly inserted successor may not be in UPDATE's snapshot.
    # The account generation kills it regardless of when it commits.
    predicate = [StaffAccount.id == account_id]
    if expected_version is not None:
        predicate.append(StaffAccount.credential_version == expected_version)
    changed = await session.execute(
        update(StaffAccount)
        .where(*predicate)
        .values(credential_version=StaffAccount.credential_version + 1)
    )
    if changed.rowcount != 1:
        return
    await session.execute(
        update(StaffAccountSession)
        .where(StaffAccountSession.account_id == account_id)
        .values(revoked_at=now)
    )
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id.in_(select(User.id).where(User.account_id == account_id)))
        .values(revoked_at=now)
    )
    await session.execute(
        update(User)
        .where(User.account_id == account_id, User.membership_pending.is_(False))
        .values(credential_version=User.credential_version + 1)
    )


async def find_cookie_session(session: AsyncSession, raw: str) -> StaffAccountSession | None:
    """The row a cookie PROVES — its current or just-replaced secret (no liveness check).

    Same parsing as `rotate_account_session`, but a stale/wrong secret proves nothing here:
    logout never acts on a value it cannot match (reuse detection lives in resume).
    """
    row, presented, _ = await _cookie_row(session, raw)
    if row is None or presented not in (row.token_hash, row.previous_token_hash):
        return None
    return row


async def end_cookie_account(session: AsyncSession, raw: str):
    if not raw:
        return
    # The just-replaced value counts too: a tab logging out while another resumed must
    # still sign the person out (logout can only take access away).
    row = await find_cookie_session(session, raw)
    if not row or aware(row.expires_at) <= datetime.now(UTC):
        return
    account = await session.scalar(
        select(StaffAccount)
        .where(StaffAccount.id == row.account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account and account.credential_version == row.credential_version:
        await end_account_sessions(session, account.id, expected_version=row.credential_version)
