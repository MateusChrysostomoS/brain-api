"""FastAPI application entrypoint.

Run with:
    uvicorn brain_api.main:app --host 0.0.0.0 --port 8000
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from brain_api.api import auth, demo, entitlements, health, sso
from brain_api.config import get_settings
from brain_api.core.logging import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info("api_starting", env=settings.APP_ENV)
    try:
        yield
    finally:
        logger.info("api_stopped")


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(
        title="Brain API",
        version="0.1.0",
        lifespan=lifespan,
    )
    # CORS for the Next.js Brain portal (origins are configurable per env).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router, tags=["health"])
    app.include_router(auth.router, tags=["auth"])
    app.include_router(entitlements.router, tags=["entitlements"])
    app.include_router(sso.router, tags=["sso"])
    app.include_router(demo.router, tags=["demo"])
    return app


app = create_app()
