"""The only source of "now" (CLAUDE.md invariant 7). Every datetime is tz-aware America/New_York."""

import asyncio
import heapq
import itertools
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
# Regular session. Early closes come from the trading calendar at runtime.
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


class NaiveDatetimeError(ValueError):
    """A datetime without tzinfo reached code that needs an absolute instant."""


def ensure_et(dt: datetime) -> datetime:
    """Return ``dt`` in ET. Naive datetimes raise instead of silently assuming a zone."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise NaiveDatetimeError(f"naive datetime {dt.isoformat()}: attach a timezone")
    return dt.astimezone(ET)


def et_datetime(d: date, t: time) -> datetime:
    """Wall-clock time ``t`` on ``d`` in ET.

    Raises for wall times skipped or repeated by a DST switch; market times never fall there.
    """
    if t.tzinfo is not None:
        raise ValueError("t must be a wall-clock time without tzinfo")
    dt = datetime.combine(d, t, tzinfo=ET)
    back = dt.astimezone(UTC).astimezone(ET)
    if (back.date(), back.time()) != (d, t):
        raise ValueError(f"{d} {t} does not exist in America/New_York (DST gap)")
    if dt.replace(fold=1).utcoffset() != dt.utcoffset():
        raise ValueError(f"{d} {t} is ambiguous in America/New_York (DST overlap)")
    return dt


def parse_et_iso(text: str) -> datetime:
    """Parse ISO-8601 text that carries a UTC offset; returns ET. Naive text is rejected."""
    return ensure_et(datetime.fromisoformat(text))


def fmt_iso(dt: datetime) -> str:
    """ISO-8601 with offset, in ET: the journal and log format."""
    return ensure_et(dt).isoformat()


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, tz-aware ET."""
        ...

    async def sleep_until(self, t: datetime) -> None:
        """Return once ``now() >= t``."""
        ...


class RealClock:
    """Wall-clock time in ET."""

    # Long sleeps wake periodically so system clock adjustments are honoured.
    _MAX_NAP_S = 30.0

    def now(self) -> datetime:
        return datetime.now(tz=ET)

    async def sleep_until(self, t: datetime) -> None:
        target = ensure_et(t)
        while True:
            remaining = (target - self.now()).total_seconds()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, self._MAX_NAP_S))


class SimClock:
    """Manually advanced clock for tests and replay. Time never moves backwards."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_et(start)
        self._sleepers: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    def now(self) -> datetime:
        return self._now

    def next_wakeup(self) -> datetime | None:
        """Deadline of the earliest pending sleeper, if any."""
        while self._sleepers and self._sleepers[0][2].done():
            heapq.heappop(self._sleepers)  # drop sleepers whose task was cancelled
        return self._sleepers[0][0] if self._sleepers else None

    async def sleep_until(self, t: datetime) -> None:
        target = ensure_et(t)
        if target <= self._now:
            await asyncio.sleep(0)  # still yield, like a real sleep
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (target, next(self._seq), fut))
        await fut

    async def advance(self, delta: timedelta) -> None:
        await self.advance_to(self._now + delta)

    async def advance_to(self, t: datetime) -> None:
        """Move time forward to ``t``, waking sleepers in deadline order (FIFO on ties).

        After each wake the event loop runs once, so a woken task reaches its next await
        (possibly scheduling another sleep within the window) before time moves again.
        """
        target = ensure_et(t)
        if target < self._now:
            raise ValueError(f"SimClock cannot go backwards: {target} < {self._now}")
        while self._sleepers and self._sleepers[0][0] <= target:
            deadline, _, fut = heapq.heappop(self._sleepers)
            if fut.done():  # its task was cancelled
                continue
            self._now = deadline
            fut.set_result(None)
            await asyncio.sleep(0)
        self._now = target
        await asyncio.sleep(0)
