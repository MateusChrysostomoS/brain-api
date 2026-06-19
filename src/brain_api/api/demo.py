"""Public demo-request endpoint (CONTRACTS.md §4 + §5): "Agendar demo" lead capture.

Isolated, synchronous lead capture. It persists at most one `demo_requests` row and
returns a fixed confirmation. It does NOT create a tenant, touch entitlements, call
Stripe, or trigger any async/queue work (CONTRACTS.md §0.4). The honeypot defends against
bots; a trivial in-process per-IP limit blunts spam (CONTRACTS.md §5).

The request body (message, email) is NEVER logged — only a stable `id` + `source`.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.core.database import get_session
from brain_api.core.logging import get_logger
from brain_api.schemas.demo import DemoRequestConfirmation, DemoRequestCreate
from brain_api.services.demo import check_rate_limit, create_demo_request

logger = get_logger(__name__)

# main.py does `app.include_router(demo.router, ...)`, so the module-level name MUST be
# `router` and the path carries the full route (no prefix).
router = APIRouter()

# Fixed confirmation copy shown to every accepted lead (CONTRACTS.md §4.1).
_CONFIRMATION_MESSAGE = "Recebemos seu pedido! Nossa equipe entra em contato em até 1 dia útil."


def _client_ip(request: Request) -> str:
    """Best-effort client IP for the per-IP limiter.

    The service runs behind nginx, so prefer the first hop of `X-Forwarded-For`
    (the original client) and fall back to the direct peer.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


@router.post(
    "/demo-requests",
    status_code=status.HTTP_201_CREATED,
    response_model=DemoRequestConfirmation,
    summary="Submit a demo request",
    description="Public lead capture for the 'Agendar demo' form. Creates no tenant.",
    responses={
        201: {"description": "Lead captured (or silently accepted for a honeypot hit)."},
        422: {"description": "Validation error (missing name/email, bad enum, oversize)."},
        429: {"description": "Rate limited (basic per-IP anti-spam)."},
    },
)
async def create_demo_request_endpoint(
    request: Request,
    payload: DemoRequestCreate,
    session: AsyncSession = Depends(get_session),
) -> DemoRequestConfirmation:
    """Capture a demo request: honeypot + rate-limit guard, then persist + confirm."""
    ip = _client_ip(request)

    # HONEYPOT (CONTRACTS.md §5): a filled hidden field means a bot. Silently
    # accept-and-drop — return a normal 201 WITHOUT persisting a row. Use a synthetic
    # nil UUID so the response shape is valid without leaking a real id.
    if payload.website and payload.website.strip():
        logger.info("demo_request_honeypot_dropped", source=str(payload.source or "brain"))
        return DemoRequestConfirmation(
            id="00000000-0000-0000-0000-000000000000",
            status="new",
            message=_CONFIRMATION_MESSAGE,
        )

    # RATE LIMIT (CONTRACTS.md §5): trip -> 429, fail-open inside check_rate_limit.
    if not check_rate_limit(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Muitas solicitações. Tente novamente em instantes.",
        )

    row = await create_demo_request(session, payload)
    # Stable reference only — never log the message body or email.
    logger.info("demo_request_created", id=str(row.id), source=row.source)
    return DemoRequestConfirmation(
        id=row.id,
        status=row.status,
        message=_CONFIRMATION_MESSAGE,
    )
