"""MarketDataHub: the one market-data WebSocket, shared by both books (A§1, A§4.2).

Trades, quotes and minute bars for at most ``stream_symbol_limit`` symbols are parsed, fanned
out to consumer queues, and kept as "latest" per symbol for freshness checks (R-016). After a
reconnect, missed minute bars are backfilled over REST and published with
``source="backfill"``.

``HubState`` holds what a hub knows and whom it tells; the replay hub (``data/replay.py``) uses
the same class, so live and replayed days behave alike. Taps are called synchronously at publish
time (DECISIONS D-047): the recorder stamps each event's receive time there, and SimBroker sees
each event at the instant it arrives.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict

from trader.core.clock import Clock, parse_et_iso
from trader.core.models import Bar, Quote, Trade
from trader.data.conditions import counts_for_price
from trader.data.ws import (
    STOCK_STREAM_URL,
    AlpacaStream,
    StreamError,
    WsConnector,
    websockets_connector,
)

log = structlog.get_logger(__name__)

StreamFeed = Literal["iex", "sip", "test"]
Channel = Literal["trades", "quotes", "bars"]
CHANNELS: tuple[Channel, ...] = ("trades", "quotes", "bars")
TRADING_CHANNELS: tuple[Channel, ...] = ("trades", "quotes")  # run-day needs no bars (D-048)
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


Tap = Callable[[MarketEvent], None]
PRICE_MAX_AGE_S = 60.0  # an older last trade is no price at all: callers use the broker's


class HubState:
    """What a hub knows (latest trade and quote, event ages) and whom it tells (consumer
    queues and synchronous taps)."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._consumers: list[asyncio.Queue[MarketEvent]] = []
        self._taps: list[Tap] = []
        self._last_trade: dict[str, Trade] = {}
        self._last_quote: dict[str, Quote] = {}
        self._priced: dict[str, tuple[Trade, datetime]] = {}  # trades that count, when received
        self._last_seen: dict[str, datetime] = {}
        self._last_any: datetime | None = None

    def subscribe_consumer(self) -> asyncio.Queue[MarketEvent]:
        queue: asyncio.Queue[MarketEvent] = asyncio.Queue()
        self._consumers.append(queue)
        return queue

    def remove_consumer(self, queue: asyncio.Queue[MarketEvent]) -> None:
        """Stop feeding a queue nobody reads any more, so it can't grow without bound."""
        with contextlib.suppress(ValueError):
            self._consumers.remove(queue)

    def add_tap(self, tap: Tap) -> None:
        self._taps.append(tap)

    def publish(self, event: MarketEvent, *, seen: bool = True) -> None:
        now = self._clock.now()
        if isinstance(event, Trade):
            self._last_trade[event.symbol] = event
            if counts_for_price(event):
                self._priced[event.symbol] = (event, now)
        elif isinstance(event, Quote):
            self._last_quote[event.symbol] = event
        if seen:
            self._last_seen[event.symbol] = self._last_any = now
        for tap in list(self._taps):
            try:
                tap(event)
            except Exception:
                log.exception("hub tap failed")
        for queue in self._consumers:
            queue.put_nowait(event)

    def last_trade(self, symbol: str) -> Trade | None:
        return self._last_trade.get(symbol)

    def last_quote(self, symbol: str) -> Quote | None:
        return self._last_quote.get(symbol)

    def last_price(self, symbol: str, max_age_s: float = PRICE_MAX_AGE_S) -> Decimal | None:
        """The latest trade that may set a price (odd lots excluded, D-043), or None once it is
        older than ``max_age_s``: a stale price must not hide a loss (``PriceSource``)."""
        found = self._priced.get(symbol)
        if found is None or (self._clock.now() - found[1]).total_seconds() > max_age_s:
            return None
        return found[0].price

    def last_event_age_s(self, symbol: str | None = None) -> float:
        seen = self._last_any if symbol is None else self._last_seen.get(symbol)
        return float("inf") if seen is None else (self._clock.now() - seen).total_seconds()


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
        channels: Sequence[Channel] = CHANNELS,
        on_stream_error: Callable[[StreamError], None] | None = None,
    ) -> None:
        """``channels``: what ``set_subscriptions`` asks for unless told otherwise.
        ``on_stream_error``: server errors no request was waiting for (a 405 on the
        subscriptions replayed after a reconnect)."""
        self._clock, self._limit, self._backfill = clock, symbol_limit, backfill
        self._channels = tuple(channels)
        self._symbols: set[str] = set()
        self._state = HubState(clock)
        self._last_bar_ts: dict[str, datetime] = {}
        self._task: asyncio.Task[None] | None = None
        self._stream = AlpacaStream(
            url=STOCK_STREAM_URL.format(feed=feed),
            key_id=key_id,
            secret_key=secret_key,
            channels=CHANNELS,
            clock=clock,
            on_data=self._on_data,
            on_connected=self._on_connected,
            on_error=on_stream_error,
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

    async def set_subscriptions(
        self, symbols: Iterable[str], *, channels: Sequence[Channel] | None = None
    ) -> dict[str, list[str]]:
        """Stream ``channels`` (default: the hub's) for exactly ``symbols``; every other
        channel is dropped. Returns the server's ack.

        Raises SubscriptionLimitError above ``stream_symbol_limit`` without asking the server,
        and StreamError when the server rejects the change (its previous set stays).
        """
        wanted = {s.upper() for s in symbols}
        if len(wanted) > self._limit:
            raise SubscriptionLimitError(
                f"{len(wanted)} symbols exceeds the limit of {self._limit}"
            )
        chosen = self._channels if channels is None else tuple(channels)
        acked = await self._stream.update(
            {channel: wanted if channel in chosen else set() for channel in CHANNELS}
        )
        self._symbols = wanted
        return acked

    def subscribe_consumer(self) -> asyncio.Queue[MarketEvent]:
        """A queue that receives every event from now on."""
        return self._state.subscribe_consumer()

    def remove_consumer(self, queue: asyncio.Queue[MarketEvent]) -> None:
        self._state.remove_consumer(queue)

    def add_tap(self, tap: Tap) -> None:
        """Call ``tap`` synchronously for every event, before any consumer queue sees it."""
        self._state.add_tap(tap)

    # ---------------------------------------------------------------- state

    def last_trade(self, symbol: str) -> Trade | None:
        return self._state.last_trade(symbol)

    def last_quote(self, symbol: str) -> Quote | None:
        return self._state.last_quote(symbol)

    def last_price(self, symbol: str, max_age_s: float = PRICE_MAX_AGE_S) -> Decimal | None:
        return self._state.last_price(symbol, max_age_s)

    def last_event_age_s(self, symbol: str | None = None) -> float:
        """Seconds since the last event for ``symbol`` (any symbol if None); inf if none yet."""
        return self._state.last_event_age_s(symbol)

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(self._symbols)

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
        elif kind == "q":
            event = Quote(
                symbol=symbol,
                ts=parse_et_iso(message["t"]),
                bid=message["bp"],
                bid_size=int(message["bs"]),
                ask=message["ap"],
                ask_size=int(message["as"]),
            )
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
        self._state.publish(event)

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
                    backfilled = BarEvent(symbol=symbol, bar=bar, source="backfill")
                    self._state.publish(backfilled, seen=False)
                    published += 1
        log.info("stream backfilled", since=since.isoformat(), bars=published)

    def _note_bar(self, symbol: str, ts: datetime) -> None:
        last = self._last_bar_ts.get(symbol)
        if last is None or ts > last:
            self._last_bar_ts[symbol] = ts
