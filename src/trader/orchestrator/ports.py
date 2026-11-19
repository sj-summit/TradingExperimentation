"""What the day needs from the outside world (A§1): one market-data hub, the REST market data and,
for Book B, news, filings, packs and the review (D-069).

``MarketDataHub`` and ``ReplayHub`` are hubs; ``AlpacaRest``, ``RecordingMarketData`` and
``ReplayMarketData`` are day markets. Brokers are ``BrokerAdapter``s. ``NewsHub`` is a news feed,
``EdgarClient`` a filings port. ``orchestrator.packs`` holds the pack sources.
"""

import asyncio
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from trader.core.models import (
    Asset,
    Bar,
    Filing,
    NewsItem,
    Quote,
    Signal,
    SipTrade,
    Snapshot,
    Split,
    Trade,
)
from trader.data.alpaca_rest import Adjustment, Feed, Timeframe
from trader.data.stream import Channel, MarketEvent, Tap
from trader.llm.client import LLMResult
from trader.llm.context import BookB, Common, EncodedPack, Pack


class Hub(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def wait_connected(self, timeout_s: float) -> bool: ...
    async def set_subscriptions(
        self, symbols: Iterable[str], *, channels: Sequence[Channel] | None = None
    ) -> dict[str, list[str]]: ...
    def subscribe_consumer(self) -> asyncio.Queue[MarketEvent]: ...
    def remove_consumer(self, queue: asyncio.Queue[MarketEvent]) -> None: ...
    def add_tap(self, tap: Tap) -> None: ...
    def last_trade(self, symbol: str) -> Trade | None: ...
    def last_quote(self, symbol: str) -> Quote | None: ...
    def last_price(self, symbol: str, max_age_s: float = ...) -> Decimal | None: ...
    def last_event_age_s(self, symbol: str | None = None) -> float: ...
    @property
    def symbols(self) -> frozenset[str]: ...


class DayMarket(Protocol):
    """The REST market data a day reads after boot (each call is recorded for replay)."""

    async def clock(self) -> datetime: ...
    async def assets(
        self, *, status: Literal["active", "inactive"] | None = "active"
    ) -> list[Asset]: ...
    async def bars(
        self,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
        *,
        feed: Feed,
        adjustment: Adjustment = "raw",
    ) -> dict[str, list[Bar]]: ...
    async def snapshots(self, symbols: Sequence[str], *, feed: Feed) -> dict[str, Snapshot]: ...
    async def splits(self, start: date, end: date) -> list[Split]: ...
    async def trades(
        self, symbols: Sequence[str], start: datetime, end: datetime, *, feed: Feed
    ) -> dict[str, list[SipTrade]]: ...


# ---------------------------------------------------------------- Book B (D-069)


class NewsPort(Protocol):
    """The stored news: a read of the ``news`` table, never a network call."""

    async def recent(self, symbols: Iterable[str], since: datetime) -> list[NewsItem]: ...


class NewsFeed(NewsPort, Protocol):
    """``NewsHub``: started at boot (a REST backfill, then the stream), stopped at the end."""

    async def start(self, lookback: timedelta) -> int: ...
    async def stop(self) -> None: ...


class FilingsPort(Protocol):
    """``EdgarClient``: a symbol's flagged filings in the last ``days`` days, newest first."""

    async def recent_filings(self, symbol: str, days: int) -> list[Filing]: ...


class PackSource(Protocol):
    """Where Book B's jobs get their encoded packs: built from the day (``LivePacks``), recorded
    on the way (``RecordingPacks``), or served from a recording (``ReplayPacks``)."""

    async def premarket_scan(self, *, edgar_until: datetime) -> None:
        """The 09:00 scan: pick the brief's candidates and prefetch their filings."""
        ...

    async def brief(self, *, book_b: BookB) -> EncodedPack: ...

    async def vet(
        self, signals: Sequence[Signal], *, book_b: BookB, edgar_until: datetime
    ) -> EncodedPack: ...

    async def review(self, *, book_b: BookB) -> EncodedPack | None:
        """The review's pack; None when there is none to make (no review work, no targets)."""
        ...


class ReviewWork(Protocol):
    """The review's content, from ``memory/reviews.py`` (P4.10): its pack, the transaction that
    stores a good answer, and the record of a review that didn't come."""

    async def pack(self, *, as_of: datetime, common: Common) -> Pack | None: ...

    def persist(self, conn: sqlite3.Connection, output: BaseModel, attempt_id: str) -> None: ...

    async def not_reviewed(self, *, why: str, result: LLMResult[Any] | None) -> None: ...
