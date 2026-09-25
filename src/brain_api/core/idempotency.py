"""Idempotent patient sends — a retried message is relayed ONCE (2026-09-25).

A phone on a mobile network loses a response more often than a request: the message reached
the product, the answer never reached the browser, and the portal's automatic retry would
deliver the same answer to the questionnaire twice. The client names each message with an
`Idempotency-Key` (a UUID it generates once per message and keeps across its retries); this
module remembers what that key already produced, for one patient on one thread.

WHAT IS REMEMBERED, AND WHAT IS NOT:
  * a SUCCESS is kept for `TTL_SECONDS` and replayed as is — the product is not called again;
  * a FAILURE is never kept: the entry is dropped and the next retry relays for real. A 4xx
    was refused by the product (nothing was delivered), a 502/503 most often never reached it;
  * a retry that arrives WHILE the first attempt is still in flight waits for it and gets
    ITS outcome — the usual shape of "the client timed out, the server did not".

IN MEMORY, ON PURPOSE, AND ITS LIMITS. brain-api runs as ONE uvicorn process (Dockerfile
`CMD`), and a retry window is seconds to minutes, so a process-local map covers the case this
exists for without a migration or a new service. A restart inside the window forgets the
keys (a retry then relays again — today's behaviour, never worse), and a second replica
would need this moved to Postgres or Redis. Same stance as the rate limiters in
`core/ratelimit.py`.

A key is scoped to (patient, product): two patients — or two tabs of one clinic — can never
collide, and a key is not a credential for anything.
"""

import asyncio
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
#: How long a delivered message is remembered. Covers a client's whole retry ladder
#: (seconds) with room for a patient pressing "tentar novamente" minutes later.
TTL_SECONDS = 10 * 60
#: Bound on memory: past this, the oldest entries go first (each is a small relay payload).
MAX_ENTRIES = 10_000
#: A UUID fits; anything that could smuggle a separator or a path does not.
_KEY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

T = TypeVar("T")


def is_valid_key(value: str) -> bool:
    return _KEY.fullmatch(value) is not None


@dataclass
class _Entry:
    future: asyncio.Future
    expires_at: float = field(default=0.0)


class IdempotencyCache:
    """(scope, key) → the one outcome of that message, in flight or delivered."""

    def __init__(self, ttl: float = TTL_SECONDS, max_entries: int = MAX_ENTRIES) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()

    async def run(self, scope: str, key: str, send: Callable[[], Awaitable[T]]) -> tuple[T, bool]:
        """`(result, replayed)`: `send()` runs at most once per live key; errors are not kept.

        No lock is needed: the event loop is single-threaded and nothing between the lookup
        and the insert awaits, so two concurrent requests cannot both miss.
        """
        self._evict(time.monotonic())
        slot = (scope, key)
        while (entry := self._entries.get(slot)) is not None:
            try:
                # `shield`: a caller that goes away must not cancel the ORIGINAL send.
                return await asyncio.shield(entry.future), True
            except asyncio.CancelledError:
                if not entry.future.cancelled():
                    raise  # THIS request was cancelled — not the original
                # The original was cancelled before it finished; loop and take over the send.

        entry = _Entry(future=asyncio.get_running_loop().create_future())
        self._entries[slot] = entry
        try:
            result = await send()
        except asyncio.CancelledError:
            self._entries.pop(slot, None)
            entry.future.cancel()
            raise
        except Exception as exc:
            # Not remembered: the next retry relays for real. A waiter already attached gets
            # this same outcome; with none, the exception is retrieved here so the loop never
            # logs "Future exception was never retrieved".
            self._entries.pop(slot, None)
            entry.future.set_exception(exc)
            entry.future.exception()
            raise
        entry.expires_at = time.monotonic() + self._ttl
        entry.future.set_result(result)
        return result, False

    def _evict(self, now: float) -> None:
        # Oldest first: entries are inserted in time order, so the expired ones sit at the
        # front. An in-flight entry stops the sweep; the size bound below still holds.
        while self._entries:
            entry = next(iter(self._entries.values()))
            if not (entry.future.done() and entry.expires_at <= now):
                break
            self._entries.popitem(last=False)
        while len(self._entries) >= self._max:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


#: The process-wide instance the send route uses.
patient_sends = IdempotencyCache()
