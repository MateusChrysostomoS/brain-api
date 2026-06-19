"""Seed a development tenant + owner user + entitlement.

Idempotent: re-running does nothing once the demo user exists. Dev-only credentials:

    email:    dra.demo@clinica.com.br
    password: demo1234

Run with:  make seed   (or)   uv run python scripts/seed_dev.py
"""

import asyncio
import os

from sqlalchemy import select

from brain_api.core.database import async_session_factory
from brain_api.core.logging import get_logger, setup_logging
from brain_api.core.security import hash_password
from brain_api.models import Entitlement, PrecheckAccountLink, Tenant, User
from brain_api.models.user import ROLE_TENANT_OWNER

setup_logging()
logger = get_logger(__name__)

DEMO_EMAIL = "dra.demo@clinica.com.br"
DEMO_PASSWORD = "demo1234"
DEMO_CLINIC = "Consultório Dr. Aurélio Lima"


async def seed() -> None:
    async with async_session_factory() as session, session.begin():
        existing = await session.scalar(select(User).where(User.email == DEMO_EMAIL))
        if existing is not None:
            logger.info("seed_skipped_user_exists", email=DEMO_EMAIL)
            return

        tenant = Tenant(clinic_name=DEMO_CLINIC)
        session.add(tenant)
        await session.flush()  # assign tenant.id

        session.add(
            User(
                tenant_id=tenant.id,
                email=DEMO_EMAIL,
                name="Dra. Demo",
                password_hash=hash_password(DEMO_PASSWORD),
                role=ROLE_TENANT_OWNER,
            )
        )
        session.add(
            Entitlement(
                tenant_id=tenant.id,
                precheck_enabled=True,
                secretaria_enabled=True,
                plan="brain-completo",
                status="active",
            )
        )

    logger.info("seed_complete", email=DEMO_EMAIL, clinic=DEMO_CLINIC)


async def ensure_demo_link() -> None:
    """Optionally link the demo user to a PreCheck user for a full local SSO E2E.

    Off by default — only acts when `DEMO_PRECHECK_USER_ID` is set (the integer id of a
    real user in PreCheck's OWN database). Mirrors scripts/link_precheck_account.py but
    scoped to the demo user, so `make seed` can wire SSO end-to-end in one step. Idempotent.
    """
    raw = os.getenv("DEMO_PRECHECK_USER_ID")
    if not raw:
        return
    try:
        precheck_user_id = int(raw)
    except ValueError:
        logger.warning("seed_link_skipped_bad_id", value=raw)
        return

    async with async_session_factory() as session, session.begin():
        user = await session.scalar(select(User).where(User.email == DEMO_EMAIL))
        if user is None or user.tenant_id is None:
            logger.warning("seed_link_skipped_no_demo_user")
            return
        existing = await session.scalar(
            select(PrecheckAccountLink).where(PrecheckAccountLink.brain_user_id == user.id)
        )
        if existing is not None:
            existing.precheck_user_id = precheck_user_id
            existing.tenant_id = user.tenant_id
        else:
            session.add(
                PrecheckAccountLink(
                    brain_user_id=user.id,
                    precheck_user_id=precheck_user_id,
                    tenant_id=user.tenant_id,
                )
            )

    logger.info("seed_link_ready", email=DEMO_EMAIL, precheck_user_id=precheck_user_id)


async def main() -> None:
    await seed()
    await ensure_demo_link()


if __name__ == "__main__":
    asyncio.run(main())
