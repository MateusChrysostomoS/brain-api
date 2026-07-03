"""Auth service layer — credential verification, identity lookups, refresh tokens.

Mutable/sensitive state (the user row, the tenant) is read server-side here, never
trusted from the token. Decryption is not relevant in this scope (brain-api stores no
tenant secrets yet); `password_hash` is verified in-memory and never returned.

Refresh tokens (auth-jwt-multitenant): opaque values, stored HASHED (sha256), rotated
on every use. A presented token that was already rotated (revoked) is treated as reuse
— the whole active family for that user is revoked. Raw token values are never logged.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.core.security import (
    generate_refresh_token,
    hash_refresh_token,
    verify_password,
)
from brain_api.models import RefreshToken, Tenant, User

logger = get_logger(__name__)


async def authenticate(session: AsyncSession, email: str, password: str) -> User | None:
    """Return the user iff the email (case-insensitive) and password both match.

    Email is looked up lower-cased (users are stored lower-cased). An unknown email
    and a bad password yield the SAME `None` result — callers must not distinguish the
    two (CONTRACTS.md §2.1: one "Credenciais inválidas" message for both).
    """
    user = await session.scalar(select(User).where(User.email == email.lower()))
    if user is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def get_user(session: AsyncSession, user_id: UUID) -> User | None:
    """Load a user by primary key (or None if it no longer exists)."""
    return await session.scalar(select(User).where(User.id == user_id))


async def get_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant | None:
    """Load a tenant by primary key (or None if it no longer exists)."""
    return await session.scalar(select(Tenant).where(Tenant.id == tenant_id))


# --- Refresh tokens ---------------------------------------------------------


def _as_utc(dt: datetime) -> datetime:
    """Normalize a DB datetime for comparison (SQLite returns naive; Postgres aware)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def issue_refresh_token(session: AsyncSession, user_id: UUID) -> str:
    """Create + persist a refresh token for the user; return the RAW value (once).

    Only the sha256 hash is stored. Commits (mirrors the write pattern of the other
    service layers — the request session is not auto-committing).
    """
    raw = generate_refresh_token()
    session.add(
        RefreshToken(
            user_id=user_id,
            token_hash=hash_refresh_token(raw),
            expires_at=datetime.now(UTC)
            + timedelta(days=get_settings().REFRESH_TOKEN_EXPIRE_DAYS),
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

    now = datetime.now(UTC)
    if row.revoked_at is not None:
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == row.user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await session.commit()
        # Stable ids only — never the token value.
        logger.warning("refresh_token_reuse_detected", user_id=str(row.user_id))
        return None
    if _as_utc(row.expires_at) <= now:
        return None

    user = await session.get(User, row.user_id)
    if user is None:
        row.revoked_at = now
        await session.commit()
        return None

    # Rotate: kill the presented token, mint its successor (same commit).
    row.revoked_at = now
    new_raw = generate_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
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
