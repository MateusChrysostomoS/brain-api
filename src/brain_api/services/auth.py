"""Auth service layer — credential verification and identity lookups.

Mutable/sensitive state (the user row, the tenant) is read server-side here, never
trusted from the token. Decryption is not relevant in this scope (brain-api stores no
tenant secrets yet); `password_hash` is verified in-memory and never returned.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.core.security import verify_password
from brain_api.models import Tenant, User


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
