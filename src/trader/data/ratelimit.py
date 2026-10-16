"""Async token-bucket rate limiter, one per REST host (ARCHITECTURE.md §4.2)."""

import asyncio
import time
from collections.abc import Awaitable, Callable

# Refill arithmetic leaves float dust (0.9999999…); treat that as a whole token, and never wait
# less than a millisecond, so the bucket can't spin on near-zero sleeps.
_EPSILON = 1e-9
_MIN_WAIT_S = 0.001


class RateLimiter:
    """At most ``capacity`` requests in a burst, refilled at ``rate_per_min`` per minute.

    Any 60-second window therefore sees at most ``capacity + rate_per_min`` requests: keep that
    under the server's limit. Routine callers are served in arrival order and never take the
    bucket below ``reserve`` tokens. An ``urgent`` caller (an order submit, cancel or sell:
    DECISIONS D-056) may use the reserve, and goes ahead of every routine caller waiting.
    ``pause_for`` holds every caller, e.g. after a 429.
    """

    def __init__(
        self,
        rate_per_min: float,
        *,
        capacity: int = 10,
        reserve: int = 0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_min <= 0 or capacity < 1:
            raise ValueError("rate_per_min must be > 0 and capacity >= 1")
        if not 0 <= reserve < capacity:
            raise ValueError("reserve must be >= 0 and below capacity")
        self._per_s = rate_per_min / 60.0
        self._capacity = float(capacity)
        self._reserve = float(reserve)
        self._tokens = float(capacity)
        self._monotonic, self._sleep = monotonic, sleep
        self._updated = monotonic()
        self._paused_until = 0.0
        self._lock = asyncio.Lock()  # FIFO: one routine waiter at a time owns the refill wait
        self._urgent_lock = asyncio.Lock()  # FIFO among urgent callers
        self._urgent_waiting = 0

    async def acquire(self, *, urgent: bool = False) -> None:
        if urgent:
            self._urgent_waiting += 1
            try:
                async with self._urgent_lock:
                    await self._take(keep=0.0, urgent=True)
            finally:
                self._urgent_waiting -= 1
            return
        async with self._lock:
            await self._take(keep=self._reserve, urgent=False)

    async def _take(self, *, keep: float, urgent: bool) -> None:
        """One token, leaving at least ``keep``; a routine caller also lets every urgent one
        waiting go first."""
        while True:
            now = self._monotonic()
            if now < self._paused_until:
                await self._sleep(self._paused_until - now)
                continue
            self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._per_s)
            self._updated = now
            ahead = 0 if urgent else self._urgent_waiting
            need = keep + 1 + ahead
            if self._tokens >= need - _EPSILON:
                self._tokens -= 1
                return
            await self._sleep(max(_MIN_WAIT_S, (need - self._tokens) / self._per_s))

    def pause_for(self, seconds: float) -> None:
        """Hold every caller for ``seconds`` from now (extends, never shortens, a pause)."""
        self._paused_until = max(self._paused_until, self._monotonic() + seconds)
