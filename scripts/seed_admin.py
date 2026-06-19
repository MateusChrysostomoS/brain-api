"""Seed the single platform admin user (idempotent).

Reads ADMIN_EMAIL / ADMIN_PASSWORD from the environment ONLY — no admin password ever
lives in code. The admin is platform-level: `role="admin"`, `tenant_id=NULL` (admins are
not scoped to a tenant). Re-running once the admin exists is a no-op: no duplicate, no
error (the email is unique; we skip if it is already taken).

The password is bcrypt-hashed on insert and never logged; only the (non-secret) email is
logged as a stable reference.

Run with:  make seed-admin
       (or) ADMIN_EMAIL=... ADMIN_PASSWORD=... uv run python scripts/seed_admin.py
"""

import asyncio
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.database import async_session_factory
from brain_api.core.logging import get_logger, setup_logging
from brain_api.core.security import hash_password
from brain_api.models import User
from brain_api.models.user import ROLE_ADMIN

setup_logging()
logger = get_logger(__name__)


async def seed_admin(
    *,
    email: str | None = None,
    password: str | None = None,
    session_factory: Callable[[], AsyncSession] | None = None,
) -> bool:
    """Create the platform admin if it does not already exist. Idempotent.

    Credentials default to `ADMIN_EMAIL` / `ADMIN_PASSWORD` from the environment (the CLI
    path); the keyword args exist so tests can drive it against a throwaway session
    factory without touching the process environment. Returns True if a row was created,
    False if an admin with that email already existed (a no-op, no error).
    """
    settings = get_settings()
    email = (email if email is not None else settings.ADMIN_EMAIL).strip().lower()
    password = password if password is not None else settings.ADMIN_PASSWORD

    if not email or not password:
        logger.error("seed_admin_missing_env")
        raise SystemExit("ADMIN_EMAIL and ADMIN_PASSWORD must both be set")
    # bcrypt silently truncates at 72 bytes — refuse a longer secret rather than seed an
    # admin whose effective password differs from what was configured.
    if len(password.encode("utf-8")) > 72:
        raise SystemExit("ADMIN_PASSWORD must be at most 72 bytes (bcrypt limit)")

    factory = session_factory or async_session_factory
    async with factory() as session, session.begin():
        existing = await session.scalar(select(User).where(User.email == email))
        if existing is not None:
            # Idempotent: nothing to do (do not touch an existing row or raise).
            logger.info("seed_admin_skipped_exists", email=email)
            return False

        session.add(
            User(
                tenant_id=None,  # platform-level admin — no tenant scope
                email=email,
                name="Brain Co Admin",
                password_hash=hash_password(password),
                role=ROLE_ADMIN,
            )
        )

    logger.info("seed_admin_created", email=email)
    return True


if __name__ == "__main__":
    asyncio.run(seed_admin())
