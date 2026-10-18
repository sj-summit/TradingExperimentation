"""The day's LLM budget (ARCHITECTURE.md §7.2; the Phase 4 plan's decision 4; DECISIONS D-064).

Counts come from ``llm_calls``, so they survive a restart:

- ``attempts(day)``: trading CLI runs started, the first try or the repair. The day's
  ``max_calls_per_day`` caps them. Smoke calls never count, and a call refused for time or the
  cap leaves no row.
- ``failures(day)``: trading attempts that failed, which count toward Book B's
  ``llm_failures_degrade_after`` (RISK.md; plan decision 4). An attempt abandoned in a crash or a
  shutdown doesn't: the model never answered it. A refusal that would repeat on every call
  (``PERSISTENT_REFUSALS``) is journaled as an attempt that failed without a CLI run: it counts
  here, never toward the cap.

``slot`` holds ``max_concurrent`` attempts at a time. It is taken per attempt, so with calls
waiting a repair may queue behind another call's first try.
"""

import asyncio
from datetime import date

from trader.memory.journal import Journal

ABANDONED = "abandoned"
# Refusals that come back on every call until a human fixes their cause.
PERSISTENT_REFUSALS = frozenset({"sandbox_dirty", "prompt_missing", "pack_too_large"})
# The rows that are CLI runs (or ones in flight): every row but a persistent refusal's.
CLI_RUNS = "(failure IS NULL OR failure NOT IN ({}))".format(
    ", ".join(f"'{code}'" for code in sorted(PERSISTENT_REFUSALS))
)


class Budget:
    def __init__(self, journal: Journal, *, max_calls_per_day: int, max_concurrent: int) -> None:
        self._journal = journal
        self.max_calls_per_day = max_calls_per_day
        self.slot = asyncio.Semaphore(max_concurrent)

    async def attempts(self, day: date) -> int:
        row = await self._journal.fetch_one(
            f"SELECT COUNT(*) AS n FROM llm_calls WHERE date = ? AND purpose = 'trading' "
            f"AND {CLI_RUNS}",
            (day.isoformat(),),
        )
        return int(row["n"]) if row else 0

    async def spent(self, day: date) -> bool:
        return await self.attempts(day) >= self.max_calls_per_day

    async def failures(self, day: date) -> int:
        row = await self._journal.fetch_one(
            "SELECT COUNT(*) AS n FROM llm_calls WHERE date = ? AND purpose = 'trading' "
            "AND ok = 0 AND failure != ?",
            (day.isoformat(), ABANDONED),
        )
        return int(row["n"]) if row else 0
