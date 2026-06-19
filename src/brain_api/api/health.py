"""Health check endpoint."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Intentionally does not touch Postgres."""
    return {"status": "ok"}
