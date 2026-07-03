"""In-process sliding-window rate limiter (CONTRACTS.md §5).

Deliberately trivial and per-process — NO Redis. Best-effort and FAIL-OPEN: it must
never raise and never 500 the request; any internal error allows the request through
(availability over strictness, since there is no shared limiter backend in play).
State resets on restart, which is fine for anti-spam / credential-stuffing blunting.

One `SlidingWindowLimiter` instance per protected surface (demo capture, auth) so their
buckets never interfere.
"""

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import Request

from brain_api.core.logging import get_logger

logger = get_logger(__name__)

_WINDOW_SECONDS = 60.0


def client_ip(request: Request) -> str:
    """Best-effort client IP for a per-IP limiter key.

    The service runs behind nginx, so prefer the first hop of `X-Forwarded-For`
    (the original client) and fall back to the direct peer.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


class SlidingWindowLimiter:
    """Per-key (client IP) sliding 60s window. `limit_getter` is read per call so a
    settings change (or a test monkeypatch) applies without rebuilding the limiter;
    a non-positive limit disables throttling."""

    def __init__(self, name: str, limit_getter: Callable[[], int]) -> None:
        self._name = name
        self._limit_getter = limit_getter
        # key -> monotonic timestamps of recent allowed hits within the window.
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        # FastAPI may serve requests on threads; guard the shared dict.
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """True if `key` is under the limit (and records the hit), else False.

        FAIL-OPEN by contract: any unexpected error returns True (allow) rather than
        raising — the limiter must never break the request path.
        """
        try:
            limit = self._limit_getter()
            if limit <= 0:
                return True

            now = time.monotonic()
            cutoff = now - _WINDOW_SECONDS
            with self._lock:
                bucket = self._hits[key]
                while bucket and bucket[0] <= cutoff:
                    bucket.popleft()
                if len(bucket) >= limit:
                    return False
                bucket.append(now)
                return True
        except Exception:  # noqa: BLE001 - fail-open: never break the request path.
            logger.warning("rate_limit_failopen", limiter=self._name)
            return True
