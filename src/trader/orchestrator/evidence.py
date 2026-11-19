"""News and EDGAR for Book B's packs (A§10; S§5.2-5.3; the Phase 4 plan's decision 12; D-063,
D-069).

- **News** is a read of the journal's ``news`` table, which ``NewsHub`` fills (a REST backfill,
  then the stream): no network call, so no budget. An article tagged with several symbols
  counts for each. ``news_ok`` says whether the last read can be trusted: it failed, or the feed
  isn't running (it hasn't started, or its stream failed for good), and an empty list then
  means unknown, not none (``news_unavailable`` in the packs).
- **EDGAR** is asked one symbol at a time, in the order given (rank order), until a time budget
  runs out on the injected clock: the brief's prefetch until the brief, the vet's for a few
  seconds. A request still out when the budget ends is dropped.
  - A symbol EDGAR didn't answer has filings None: unknown, ``filings_unavailable`` in the pack.
    Only an answer gives a list, empty for none (D-063: the ``filings`` table can't tell "none"
    from "never asked").
  - Answers are kept for the day. The brief's pack, built at 09:05, asks nothing new. The vet
    (``refresh``) asks first for the symbols never answered, then again for the answered ones,
    which keep their earlier answer if the budget runs out: a filing accepted since 09:00 shows
    up whenever time allows.
  - A 403 or 429 pauses EDGAR for ten minutes (``EdgarClient``): nothing more is asked in that
    budget. Any other failure loses only its own symbol.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime
from typing import TypeVar

import structlog

from trader.core.clock import Clock
from trader.core.models import Filing, NewsItem
from trader.data.edgar import EdgarBlockedError
from trader.llm.context import Evidence
from trader.orchestrator.ports import FilingsPort, NewsPort

log = structlog.get_logger(__name__)

T = TypeVar("T")


class EvidenceDesk:
    def __init__(
        self,
        *,
        news: NewsPort | None,
        filings: FilingsPort | None,
        clock: Clock,
        filings_days: int,
        news_up: Callable[[], bool] | None = None,
    ) -> None:
        """``news_up``: whether the feed that fills the ``news`` table is running (``NewsHub``
        streaming or polling); None for a source that is always up."""
        self._news, self._filings, self._clock = news, filings, clock
        self._days = filings_days
        self._news_up = news_up
        self._read_ok = True
        self._answered: dict[str, tuple[Filing, ...]] = {}  # symbol -> EDGAR's answer, today

    @property
    def news_ok(self) -> bool:
        """Whether the last news read, and the feed behind it, can be trusted (the module's
        docstring): no source, a failed read or a feed that isn't running is unknown news."""
        up = self._news_up() if self._news_up is not None else True
        return self._news is not None and self._read_ok and up

    async def news(self, symbols: Iterable[str], *, since: datetime) -> dict[str, list[NewsItem]]:
        """Each symbol's stored articles created at or after ``since``, oldest first. A read
        that fails gives none, and ``news_ok`` then says so."""
        wanted = {s.upper() for s in symbols}
        if self._news is None:
            return {}
        if not wanted:
            return {}
        try:
            items = await self._news.recent(sorted(wanted), since)
        except Exception as exc:
            log.warning("news not read for a pack", error=f"{type(exc).__name__}: {exc}")
            self._read_ok = False
            return {}
        self._read_ok = True
        out: dict[str, list[NewsItem]] = {}
        for item in items:
            for symbol in dict.fromkeys(s.upper() for s in item.symbols):
                if symbol in wanted:
                    out.setdefault(symbol, []).append(item)
        return out

    async def filings(
        self, symbols: Sequence[str], *, until: datetime, refresh: bool = False
    ) -> dict[str, tuple[Filing, ...] | None]:
        """Each symbol's flagged filings, asked in order while the budget lasts; None where
        EDGAR wasn't asked in time or didn't answer. With ``refresh``, the symbols answered
        earlier are asked again once the others have been, keeping their answer if not."""
        order = list(dict.fromkeys(symbols))
        out: dict[str, tuple[Filing, ...] | None] = {s: self._answered.get(s) for s in order}
        fresh = [s for s in order if s not in self._answered]
        again = [s for s in order if s in self._answered] if refresh else []
        asked = answered = 0
        stop: str | None = None if self._filings is not None else "no EDGAR client"
        for symbol in [*fresh, *again]:
            if stop is None and self._clock.now() >= until:
                stop = "the budget ran out"
            if stop is not None or self._filings is None:
                break
            asked += 1
            try:
                found = await self._within(self._filings.recent_filings(symbol, self._days), until)
            except asyncio.CancelledError:
                raise
            except EdgarBlockedError as exc:
                stop = str(exc)
                continue
            except Exception as exc:
                log.warning("filings unavailable", symbol=symbol, error=f"{type(exc).__name__}")
                continue
            if found is None:
                stop = "the budget ran out"
                continue
            answered += 1
            self._answered[symbol] = out[symbol] = tuple(found)
        missing = sum(1 for v in out.values() if v is None)
        if asked or missing:
            log.info("edgar", asked=asked, answered=answered, unknown=missing, stopped=stop)
        return out

    async def _within(self, job: Awaitable[T], until: datetime) -> T | None:
        """``job``'s result, or None once ``until`` passes on the clock first."""
        task = asyncio.ensure_future(job)
        timer = asyncio.ensure_future(self._clock.sleep_until(until))
        try:
            await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, timer, return_exceptions=True)
        if task.cancelled():
            return None
        return task.result()


def evidence_for(
    symbols: Iterable[str],
    news: Mapping[str, Sequence[NewsItem]],
    filings: Mapping[str, Sequence[Filing] | None],
) -> dict[str, Evidence]:
    """The packs' evidence per symbol: its news, and its filings or None (unknown)."""
    return {
        s: Evidence(news=tuple(news.get(s, ())), filings=filings.get(s))
        for s in dict.fromkeys(symbols)
    }
