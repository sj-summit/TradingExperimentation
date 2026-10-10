"""The kill switch (RISK.md §6): ``runtime/KILL`` halts every book and flattens them.

The engine checks for the file every second. When it appears, every book gets a
``kill_switch`` halt and the flatten callback runs. If the callback reports that a book is
still not flat, it runs again every ``retry_s`` while the file is there. Removing the file never
resumes trading: only ``trader resume`` lifts the halts, and it refuses while the file exists.
"""

from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from pathlib import Path

import structlog

from trader.config import BookName
from trader.core.clock import Clock
from trader.risk.breakers import Halts

log = structlog.get_logger(__name__)

KILL_FILE = Path("runtime/KILL")


class KillSwitch:
    def __init__(
        self,
        path: Path,
        *,
        halts: Halts,
        clock: Clock,
        books: Sequence[BookName],
        flatten: Callable[[], Awaitable[bool]],
        poll_s: float = 1.0,
        retry_s: float = 30.0,
    ) -> None:
        """``flatten`` flattens every book and returns True once all of them are flat."""
        self._path, self._halts, self._clock = path, halts, clock
        self._books, self._flatten = tuple(books), flatten
        self._poll, self._retry = timedelta(seconds=poll_s), timedelta(seconds=retry_s)
        self._flat = False
        self._next_try = clock.now()
        self.triggered = False

    def present(self) -> bool:
        return self._path.exists()

    async def check(self) -> bool:
        """One look at the file; returns True while it exists."""
        if not self.present():
            return False
        if not self.triggered:
            self.triggered = True
            log.error("kill switch", path=str(self._path))
            for book in self._books:
                await self._halts.halt(book, "kill_switch", detail=f"{self._path} exists")
        now = self._clock.now()
        if not self._flat and now >= self._next_try:
            self._next_try = now + self._retry
            self._flat = await self._flatten()
        return True

    async def watch(self) -> None:
        """Check every ``poll_s`` until cancelled."""
        while True:
            await self.check()
            await self._clock.sleep_until(self._clock.now() + self._poll)
