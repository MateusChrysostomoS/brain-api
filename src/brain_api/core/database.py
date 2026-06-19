"""Async SQLAlchemy 2.0 engine, session factory and declarative Base."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from brain_api.config import get_settings


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""


_settings = get_settings()

# Creating the engine does NOT open a connection — it is lazy/pooled.
engine = create_async_engine(
    _settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session."""
    async with async_session_factory() as session:
        yield session
