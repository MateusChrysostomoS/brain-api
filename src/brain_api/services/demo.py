"""Demo-request service layer: in-process anti-spam + persistence (CONTRACTS.md §4 + §5).

The rate limiter is deliberately trivial and in-process — NO Redis (CONTRACTS.md §5).
It is best-effort and FAIL-OPEN: it must never raise and must never 500 the request, so
any internal error allows the request through (availability over strictness, since there
is no shared limiter backend in play).

`create_demo_request` writes exactly one `demo_requests` row. It does NOT create a
tenant, touch entitlements, or call Stripe (CONTRACTS.md §0.4 / §4.1).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.core.ratelimit import SlidingWindowLimiter
from brain_api.models import DemoRequest
from brain_api.schemas.demo import DemoRequestCreate

logger = get_logger(__name__)

_limiter = SlidingWindowLimiter("demo", lambda: get_settings().DEMO_RATE_LIMIT_PER_MIN)


def check_rate_limit(client_ip: str) -> bool:
    """Return True if `client_ip` is under the per-minute limit, else False.

    Allows up to `Settings.DEMO_RATE_LIMIT_PER_MIN` requests per 60s sliding window per
    IP (core/ratelimit.py; fail-open — the limiter must never break lead capture).
    """
    return _limiter.allow(client_ip)


async def create_demo_request(session: AsyncSession, payload: DemoRequestCreate) -> DemoRequest:
    """Persist one demo request and return the stored row.

    `source` defaults to "brain" when the client did not send it. `status` is set
    server-side by the model (defaults to "new"). The honeypot `website` field is NOT
    persisted — it never reaches this layer as a column.
    """
    row = DemoRequest(
        name=payload.name,
        email=payload.email,
        clinic=payload.clinic,
        # StrEnum values serialize to their plain string for the String() columns.
        profile=payload.profile.value if payload.profile else None,
        product_interest=(payload.product_interest.value if payload.product_interest else None),
        message=payload.message,
        source=payload.source.value if payload.source else "brain",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row
