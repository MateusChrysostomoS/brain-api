"""Seed a development tenant + owner user + entitlement.

Idempotent: re-running does nothing once the demo user exists. Dev-only credentials:

    email:    dra.demo@clinica.com.br
    password: demo1234

Run with:  make seed   (or)   uv run python scripts/seed_dev.py
"""

import asyncio

from sqlalchemy import select

from brain_api.core.database import async_session_factory
from brain_api.core.logging import get_logger, setup_logging
from brain_api.core.security import hash_password
from brain_api.models import Entitlement, Tenant, User
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


if __name__ == "__main__":
    asyncio.run(seed())
