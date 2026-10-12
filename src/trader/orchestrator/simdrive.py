"""Driving a day on a SimClock (DECISIONS D-047): replay, ``run-day --sim`` tests, scenarios.

``drive`` runs a job and, whenever nothing else can run (every task is waiting on the clock,
the journal and simulated brokers are idle), jumps the clock to the next wake-up. Time never
moves while work is pending, so a deadline can't pass in the middle of a handler, and nothing
depends on wall-clock speed. With the journal in inline mode no thread is involved at all.
"""

import asyncio
from collections.abc import Awaitable, Sequence
from typing import Protocol

from trader.core.clock import SimClock

QUIET_TURNS = 3  # consecutive loop turns with nothing ready before time may move
MAX_SETTLE_TURNS = 1_000_000


class HasIdle(Protocol):
    @property
    def idle(self) -> bool: ...


class StuckError(RuntimeError):
    """The job isn't done and nothing waits on the clock: it would never finish."""


async def settle(watched: Sequence[HasIdle] = ()) -> None:
    """Let every ready task run until the loop is quiet and every watched part is idle."""
    loop = asyncio.get_running_loop()
    quiet = 0
    for _ in range(MAX_SETTLE_TURNS):
        await asyncio.sleep(0)
        # CPython keeps runnable callbacks in ``_ready``; empty means everyone else waits.
        ready = len(getattr(loop, "_ready", ()))
        if ready == 0 and all(w.idle for w in watched):
            quiet += 1
            if quiet >= QUIET_TURNS:
                return
        else:
            quiet = 0
    raise StuckError("the event loop never went quiet")


async def drive[T](
    clock: SimClock,
    job: Awaitable[T],
    *,
    watched: Sequence[HasIdle] = (),
    max_jumps: int = 10_000_000,
) -> T:
    """Run ``job`` to completion, jumping ``clock`` to each next wake-up once all is quiet."""
    task = asyncio.ensure_future(job)
    try:
        for _ in range(max_jumps):
            await settle(watched)
            if task.done():
                return task.result()
            wake = clock.next_wakeup()
            if wake is None:
                raise StuckError("the job is not done and nothing waits on the clock")
            await clock.advance_to(wake)
        raise StuckError(f"the job did not finish within {max_jumps} clock jumps")
    finally:
        if not task.done():
            task.cancel()
