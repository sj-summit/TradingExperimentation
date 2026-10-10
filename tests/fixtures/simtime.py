"""Drive coroutines under SimClock without racing the journal's writer thread."""

import asyncio
from collections.abc import Awaitable
from datetime import datetime

from trader.core.clock import SimClock, ensure_et

REAL_STEP_S = 0.001
REAL_PATIENCE_STEPS = 3000  # about 3 s of real time for thread work between clock jumps


class JumpClock(SimClock):
    """A SimClock whose sleeps return at once, moving time to their deadline. For CLI tests,
    where nothing else could advance the clock."""

    async def sleep_until(self, t: datetime) -> None:
        await self.advance_to(max(ensure_et(t), self.now()))


async def run_on_clock[T](clock: SimClock, job: Awaitable[T], *, max_jumps: int = 10_000) -> T:
    """Run ``job`` to completion, jumping the clock to each deadline it sleeps on.

    Between jumps, real time passes in small steps until the job either finishes or waits on
    the clock again, so journal writes (on a thread) land before simulated time moves.
    """
    task = asyncio.ensure_future(job)
    try:
        for _ in range(max_jumps):
            for _ in range(REAL_PATIENCE_STEPS):
                if task.done() or clock.next_wakeup() is not None:
                    break
                await asyncio.sleep(REAL_STEP_S)
            if task.done():
                return task.result()
            wake = clock.next_wakeup()
            if wake is None:
                raise AssertionError("the job is stuck: not done and not waiting on the clock")
            await clock.advance_to(wake)
        raise AssertionError(f"the job did not finish within {max_jumps} clock jumps")
    finally:
        if not task.done():
            task.cancel()
