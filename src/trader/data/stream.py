"""MarketDataHub: the one market-data WebSocket, shared by both books (A§1, A§4.2).

Trades, quotes and minute bars for at most ``stream_symbol_limit`` symbols are parsed, fanned
out to consumer queues, and kept as "latest" per symbol for freshness checks (R-016). After a
reconnect, missed minute bars are backfilled over REST and published with
``source="backfill"``.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict

from trader.core.clock import Clock, parse_et_iso
from trader.core.models import Bar, Quote, Trade
from trader.data.ws import STOCK_STREAM_URL, AlpacaStream, WsConnector, websockets_connector

log = structlog.get_logger(__name__)

StreamFeed = Literal["iex", "sip", "test"]
CHANNELS = ("trades", "quotes", "bars")
Backfill = Callable[[Sequence[str], datetime, datetime], Awaitable[Mapping[str, Sequence[Bar]]]]


class BarEvent(BaseModel):
    """A minute bar. ``updated`` corrects an earlier bar with late trades; ``backfill`` came
    from REST after a reconnect."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    bar: Bar
    source: Literal["stream", "updated", "backfill"]


MarketEvent = Trade | Quote | BarEvent


class SubscriptionLimitError(ValueError):
    """More symbols than the plan's stream limit (30 on the free plan)."""


class MarketDataHub:
    def __init__(
        self,
        *,
        key_id: str,
        secret_key: str,
        feed: StreamFeed,
        clock: Clock,
        symbol_limit: int,
        backfill: Backfill | None = None,
        market_open: Callable[[datetime], bool] = lambda _now: False,
        connector: WsConnector = websockets_connector,
        silence_s: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._clock, self._limit, self._backfill = clock, symbol_limit, backfill
        self._symbols: set[str] = set()
        self._consumers: list[asyncio.Queue[MarketEvent]] = []
        self._last_trade: dict[str, Trade] = {}
        self._last_quote: dict[str, Quote] = {}
        self._last_bar_ts: dict[str, datetime] = {}
        self._last_seen: dict[str, datetime] = {}
        self._last_any: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._stream = AlpacaStream(
            url=STOCK_STREAM_URL.format(feed=feed),
            key_id=key_id,
            secret_key=secret_key,
            channels=CHANNELS,
            clock=clock,
            on_data=self._on_data,
            on_connected=self._on_connected,
            connector=connector,
            should_be_busy=lambda: bool(self._symbols) and market_open(clock.now()),
            silence_s=silence_s,
            sleep=sleep,
        )

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._stream.run(), name="market-data-hub")

    async def stop(self) -> None:
        await self._stream.stop()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def wait_connected(self, timeout_s: float) -> bool:
        """True once authenticated; False on timeout or a fatal stream error."""
        waiter = asyncio.ensure_future(self._stream.connected.wait())
        try:
            deadline = asyncio.get_running_loop().time() + timeout_s
            while not waiter.done():
                if self.failed is not None or asyncio.get_running_loop().time() >= deadline:
                    return False
                await asyncio.wait({waiter}, timeout=0.05)
            return True
        finally:
            waiter.cancel()

    # ---------------------------------------------------------------- subscriptions

    async def set_subscriptions(self, symbols: Iterable[str]) -> dict[str, list[str]]:
        """Stream trades, quotes and bars for exactly ``symbols``; returns the server's ack.

        Raises SubscriptionLimitError above ``stream_symbol_limit`` without asking the server,
        and StreamError when the server rejects the change (its previous set stays).
        """
        wanted = {s.upper() for s in symbols}
        if len(wanted) > self._limit:
            raise SubscriptionLimitError(
                f"{len(wanted)} symbols exceeds the limit of {self._limit}"
            )
        acked = await self._stream.update({channel: wanted for channel in CHANNELS})
        self._symbols = wanted
        return acked

    def subscribe_consumer(self) -> asyncio.Queue[MarketEvent]:
        """A queue that receives every event from now on."""
        queue: asyncio.Queue[MarketEvent] = asyncio.Queue()
        self._consumers.append(queue)
        return queue

    # ---------------------------------------------------------------- state

    def last_trade(self, symbol: str) -> Trade | None:
        return self._last_trade.get(symbol)

    def last_quote(self, symbol: str) -> Quote | None:
        return self._last_quote.get(symbol)

    def last_event_age_s(self, symbol: str | None = None) -> float:
        """Seconds since the last event for ``symbol`` (any symbol if None); inf if none yet."""
        seen = self._last_any if symbol is None else self._last_seen.get(symbol)
        return float("inf") if seen is None else (self._clock.now() - seen).total_seconds()

    @property
    def disconnected_since(self) -> datetime | None:
        return self._stream.disconnected_since

    @property
    def failed(self) -> Exception | None:
        return self._stream.failed

    @property
    def connections(self) -> int:
        return self._stream.connections

    @property
    def acked(self) -> dict[str, list[str]]:
        return self._stream.acked

    # ---------------------------------------------------------------- incoming

    def _on_data(self, message: Mapping[str, Any]) -> None:
        kind, symbol = message.get("T"), message.get("S")
        if not isinstance(symbol, str):
            return
        event: MarketEvent
        if kind == "t":
            event = Trade(
                symbol=symbol,
                ts=parse_et_iso(message["t"]),
                price=message["p"],
                size=int(message["s"]),
                conditions=tuple(message.get("c") or ()),
            )
            self._last_trade[symbol] = event
        elif kind == "q":
            event = Quote(
                symbol=symbol,
                ts=parse_et_iso(message["t"]),
                bid=message["bp"],
                bid_size=int(message["bs"]),
                ask=message["ap"],
                ask_size=int(message["as"]),
            )
            self._last_quote[symbol] = event
        elif kind in ("b", "u"):
            bar = Bar(
                ts=parse_et_iso(message["t"]),
                open=message["o"],
                high=message["h"],
                low=message["l"],
                close=message["c"],
                volume=int(message["v"]),
                trades=message.get("n"),
                vwap=message.get("vw"),
            )
            event = BarEvent(symbol=symbol, bar=bar, source="stream" if kind == "b" else "updated")
            self._note_bar(symbol, bar.ts)
        else:
            return  # statuses, LULD bands, corrections: not used yet
        now = self._clock.now()
        self._last_seen[symbol] = self._last_any = now
        self._publish(event)

    async def _on_connected(self, first: bool) -> None:
        """After a reconnect, fill the gap with REST minute bars the stream never sent."""
        since = self._stream.disconnected_since
        if first or since is None or self._backfill is None or not self._symbols:
            return
        start = since.replace(second=0, microsecond=0) - timedelta(minutes=1)
        fetched = await self._backfill(sorted(self._symbols), start, self._clock.now())
        published = 0
        for symbol in sorted(fetched):
            for bar in sorted(fetched[symbol], key=lambda b: b.ts):
                last = self._last_bar_ts.get(symbol)
                if last is None or bar.ts > last:
                    self._note_bar(symbol, bar.ts)
                    self._publish(BarEvent(symbol=symbol, bar=bar, source="backfill"))
                    published += 1
        log.info("stream backfilled", since=since.isoformat(), bars=published)

    def _note_bar(self, symbol: str, ts: datetime) -> None:
        last = self._last_bar_ts.get(symbol)
        if last is None or ts > last:
            self._last_bar_ts[symbol] = ts

    def _publish(self, event: MarketEvent) -> None:
        for queue in self._consumers:
            queue.put_nowait(event)
