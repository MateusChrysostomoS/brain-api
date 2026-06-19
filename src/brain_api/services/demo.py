"""Demo-request service layer: in-process anti-spam + persistence (CONTRACTS.md §4 + §5).

The rate limiter is deliberately trivial and in-process — NO Redis (CONTRACTS.md §5).
It is best-effort and FAIL-OPEN: it must never raise and must never 500 the request, so
any internal error allows the request through (availability over strictness, since there
is no shared limiter backend in play).

`create_demo_request` writes exactly one `demo_requests` row. It does NOT create a
tenant, touch entitlements, or call Stripe (CONTRACTS.md §0.4 / §4.1).
"""

import threading
import time
from collections import defaultdict, deque

from sqlalchemy.ext.asyncio import AsyncSession

from brain_api.config import get_settings
from brain_api.core.logging import get_logger
from brain_api.models import DemoRequest
from brain_api.schemas.demo import DemoRequestCreate

logger = get_logger(__name__)

# Sliding window length for the per-IP limit.
_WINDOW_SECONDS = 60.0

# client IP -> monotonic timestamps of recent allowed hits within the window.
# Module-level (per-process) state; this resets on restart, which is fine for a
# best-effort anti-spam control.
_hits: dict[str, deque[float]] = defaultdict(deque)
# Guards `_hits` against concurrent access (FastAPI may serve requests on threads).
_lock = threading.Lock()


def check_rate_limit(client_ip: str) -> bool:
    """Return True if `client_ip` is under the per-minute limit, else False.

    Allows up to `Settings.DEMO_RATE_LIMIT_PER_MIN` requests per 60s sliding window per
    IP. FAIL-OPEN by contract: any unexpected error returns True (allow) rather than
    raising — the limiter must never break lead capture.
    """
    try:
        limit = get_settings().DEMO_RATE_LIMIT_PER_MIN
        # A non-positive limit disables throttling (treat as unlimited / allow).
        if limit <= 0:
            return True

        now = time.monotonic()
        cutoff = now - _WINDOW_SECONDS
        with _lock:
            bucket = _hits[client_ip]
            # Drop timestamps that have aged out of the window.
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True
    except Exception:  # noqa: BLE001 - fail-open: never break the request path.
        logger.warning("demo_rate_limit_failopen")
        return True


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
