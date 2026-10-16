"""Recording and replaying a day's market data (A§4.2; DECISIONS D-047).

- ``RecordingMarketData`` wraps the REST client: every call the day makes (calendar, clock,
  assets, bars, snapshots, splits, trades) is written to ``rest.jsonl.gz`` with its arguments,
  its result (as our parsed models) or error, and its start and end receive times.
- ``ReplayMarketData`` serves those results back. Each call is matched by method and
  arguments, repeated identical calls by occurrence (a surplus call reuses the last answer and
  is counted as a divergence), and it returns after the recorded duration on the injected clock,
  so a replayed scan takes as long as it did live. A call never recorded fails loudly.
- ``ReplayHub`` replays ``stream.jsonl.gz`` by receive time through the same ``HubState`` the
  live hub uses, honoring the subscriptions.
"""

import asyncio
import contextlib
import json
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import structlog

from trader.core.clock import Clock, fmt_iso, parse_et_iso
from trader.core.models import Asset, Bar, Quote, SipTrade, Snapshot, Split, Trade
from trader.data.alpaca_rest import (
    Adjustment,
    AlpacaAuthError,
    AlpacaError,
    AlpacaForbiddenError,
    AlpacaRest,
    Feed,
    SipRecentError,
    Timeframe,
)
from trader.data.recorder import read_jsonl
from trader.data.stream import (
    PRICE_MAX_AGE_S,
    TRADING_CHANNELS,
    BarEvent,
    Channel,
    HubState,
    MarketEvent,
    SubscriptionLimitError,
    Tap,
)

log = structlog.get_logger(__name__)

Sink = Callable[[dict[str, Any]], None]


class ReplayMissError(LookupError):
    """Replay asked for something the recording doesn't have."""


# ---------------------------------------------------------------- encoding


def call_key(method: str, args: Mapping[str, Any]) -> str:
    """A canonical key: symbol lists sorted, times and dates as ISO strings."""
    return method + json.dumps({k: _canon(v) for k, v in sorted(args.items())}, sort_keys=True)


def _canon(value: Any) -> Any:
    if isinstance(value, datetime):
        return fmt_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted(str(v) for v in value)
    return value


def _encode(method: str, result: Any) -> Any:
    if method == "clock":
        return fmt_iso(result)
    if method in ("bars", "trades"):
        return {s: [b.model_dump(mode="json") for b in rows] for s, rows in result.items()}
    if method == "snapshots":
        return {s: snap.model_dump(mode="json") for s, snap in result.items()}
    if method in ("assets", "splits"):
        return [item.model_dump(mode="json") for item in result]
    if method == "asset":
        return None if result is None else result.model_dump(mode="json")
    return result  # calendar rows are plain JSON already


def _decode(method: str, data: Any) -> Any:
    if method == "clock":
        return parse_et_iso(data)
    if method == "bars":
        return {s: [Bar.model_validate(b) for b in bars] for s, bars in data.items()}
    if method == "trades":
        return {s: [SipTrade.model_validate(t) for t in rows] for s, rows in data.items()}
    if method == "snapshots":
        return {s: Snapshot.model_validate(snap) for s, snap in data.items()}
    if method == "assets":
        return [Asset.model_validate(a) for a in data]
    if method == "splits":
        return [Split.model_validate(s) for s in data]
    if method == "asset":
        return None if data is None else Asset.model_validate(data)
    return data


_ERRORS: dict[str, type[AlpacaError]] = {
    "AlpacaError": AlpacaError,
    "AlpacaAuthError": AlpacaAuthError,
    "AlpacaForbiddenError": AlpacaForbiddenError,
    "SipRecentError": SipRecentError,
}


# ---------------------------------------------------------------- recording


class RecordingMarketData:
    """The REST client, with every call written to the day's ``rest.jsonl.gz``."""

    def __init__(self, inner: AlpacaRest, clock: Clock, sink: Sink) -> None:
        self._inner, self._clock, self._sink = inner, clock, sink

    async def calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        return await self._call("calendar", {"start": start, "end": end},
                                lambda: self._inner.calendar(start, end))  # fmt: skip

    async def clock(self) -> datetime:
        return await self._call("clock", {}, self._inner.clock)

    async def assets(
        self, *, status: Literal["active", "inactive"] | None = "active"
    ) -> list[Asset]:
        return await self._call("assets", {"status": status},
                                lambda: self._inner.assets(status=status))  # fmt: skip

    async def asset(self, symbol: str) -> Asset | None:
        return await self._call("asset", {"symbol": symbol}, lambda: self._inner.asset(symbol))

    async def bars(
        self,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
        *,
        feed: Feed,
        adjustment: Adjustment = "raw",
    ) -> dict[str, list[Bar]]:
        args = {"symbols": list(symbols), "timeframe": timeframe, "start": start, "end": end,
                "feed": feed, "adjustment": adjustment}  # fmt: skip
        return await self._call(
            "bars",
            args,
            lambda: self._inner.bars(
                symbols, timeframe, start, end, feed=feed, adjustment=adjustment
            ),
        )

    async def trades(
        self, symbols: Sequence[str], start: datetime, end: datetime, *, feed: Feed
    ) -> dict[str, list[SipTrade]]:
        args = {"symbols": list(symbols), "start": start, "end": end, "feed": feed}
        return await self._call(
            "trades", args, lambda: self._inner.trades(symbols, start, end, feed=feed)
        )

    async def snapshots(self, symbols: Sequence[str], *, feed: Feed) -> dict[str, Snapshot]:
        return await self._call("snapshots", {"symbols": list(symbols), "feed": feed},
                                lambda: self._inner.snapshots(symbols, feed=feed))  # fmt: skip

    async def splits(self, start: date, end: date) -> list[Split]:
        return await self._call("splits", {"start": start, "end": end},
                                lambda: self._inner.splits(start, end))  # fmt: skip

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def _call[T](
        self, method: str, args: Mapping[str, Any], call: Callable[[], Awaitable[T]]
    ) -> T:
        started = self._clock.now()
        record: dict[str, Any] = {"method": method, "key": call_key(method, args),
                                  "start_rx": fmt_iso(started)}  # fmt: skip
        try:
            result = await call()
        except AlpacaError as exc:
            record |= {
                "end_rx": fmt_iso(self._clock.now()),
                "error": {"type": type(exc).__name__, "status": exc.status,
                          "message": exc.message, "path": exc.path, "code": exc.code},
            }  # fmt: skip
            self._sink(record)
            raise
        record |= {"end_rx": fmt_iso(self._clock.now()), "result": _encode(method, result)}
        self._sink(record)
        return result


# ---------------------------------------------------------------- replay


class ReplayMarketData:
    """Serves a recorded day's REST answers back on the injected clock."""

    def __init__(
        self,
        records: Iterable[Mapping[str, Any]],
        clock: Clock,
        *,
        since: datetime | None = None,
    ) -> None:
        """``since``: answers recorded before it are ignored. They belong to an attempt that
        died before writing its boot record, and would be served to this one (D-051)."""
        self._clock = clock
        self._calls: dict[str, deque[Mapping[str, Any]]] = defaultdict(deque)
        self.skipped = 0
        for record in records:
            if since is not None and parse_et_iso(record["start_rx"]) < since:
                self.skipped += 1
                continue
            self._calls[str(record["key"])].append(record)
        self._last: dict[str, Mapping[str, Any]] = {}
        self.divergences: list[str] = []

    @classmethod
    def from_file(
        cls, path: Path, clock: Clock, *, since: datetime | None = None
    ) -> "ReplayMarketData":
        return cls(read_jsonl(path) if path.exists() else [], clock, since=since)

    async def calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._serve("calendar", {"start": start, "end": end})
        return result

    async def clock(self) -> datetime:
        """Alpaca's recorded time, moved by however far this ask is from the recorded one, so
        the replay reads the same skew live read, whenever it asks (D-051)."""
        asked = self._clock.now()
        result, record = await self._answer("clock", {})
        moved: datetime = result + (asked - parse_et_iso(record["start_rx"]))
        return moved

    async def assets(
        self, *, status: Literal["active", "inactive"] | None = "active"
    ) -> list[Asset]:
        result: list[Asset] = await self._serve("assets", {"status": status})
        return result

    async def asset(self, symbol: str) -> Asset | None:
        result: Asset | None = await self._serve("asset", {"symbol": symbol})
        return result

    async def bars(
        self,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
        *,
        feed: Feed,
        adjustment: Adjustment = "raw",
    ) -> dict[str, list[Bar]]:
        args = {"symbols": list(symbols), "timeframe": timeframe, "start": start, "end": end,
                "feed": feed, "adjustment": adjustment}  # fmt: skip
        result: dict[str, list[Bar]] = await self._serve("bars", args)
        return result

    async def trades(
        self, symbols: Sequence[str], start: datetime, end: datetime, *, feed: Feed
    ) -> dict[str, list[SipTrade]]:
        args = {"symbols": list(symbols), "start": start, "end": end, "feed": feed}
        result: dict[str, list[SipTrade]] = await self._serve("trades", args)
        return result

    async def snapshots(self, symbols: Sequence[str], *, feed: Feed) -> dict[str, Snapshot]:
        result: dict[str, Snapshot] = await self._serve(
            "snapshots", {"symbols": list(symbols), "feed": feed}
        )
        return result

    async def splits(self, start: date, end: date) -> list[Split]:
        result: list[Split] = await self._serve("splits", {"start": start, "end": end})
        return result

    async def aclose(self) -> None:
        return None

    async def _serve(self, method: str, args: Mapping[str, Any]) -> Any:
        result, _ = await self._answer(method, args)
        return result

    async def _answer(self, method: str, args: Mapping[str, Any]) -> tuple[Any, Mapping[str, Any]]:
        """The recorded result (or error) of the next matching call, and its record."""
        key = call_key(method, args)
        queue = self._calls.get(key)
        if queue:
            record = queue.popleft()
            self._last[key] = record
        elif key in self._last:
            record = self._last[key]
            self.divergences.append(f"{method}: more calls than recorded ({key})")
        else:
            raise ReplayMissError(f"the recording has no {method} call {key}")
        took = parse_et_iso(record["end_rx"]) - parse_et_iso(record["start_rx"])
        if took > timedelta(0):  # a zero-time call never yielded when it was recorded
            await self._clock.sleep_until(self._clock.now() + took)
        if "error" in record:
            err = record["error"]
            kind = _ERRORS.get(err["type"], AlpacaError)
            status, path = int(err["status"]), str(err["path"])
            # Recordings before D-057 stored the formatted text: drop the prefix the
            # constructor adds again, so the replayed error reads as the live one.
            prefix = f"Alpaca {path}: " + (f"HTTP {status}: " if status else "")
            message = str(err["message"]).removeprefix(prefix)
            raise kind(status, message, path, err.get("code"))
        return _decode(method, record["result"]), record


@dataclass(frozen=True)
class RecordedEvent:
    rx: datetime
    event: MarketEvent


def load_stream(path: Path) -> list[RecordedEvent]:
    """``stream.jsonl.gz`` as events in receive order (exchange time if ``rx`` is missing)."""
    out: list[RecordedEvent] = []
    if not path.exists():
        return out
    for record in read_jsonl(path):
        kind = record.pop("kind")
        rx_text = record.pop("rx", None)
        event: MarketEvent
        if kind == "trade":
            event = Trade.model_validate(record)
        elif kind == "quote":
            event = Quote.model_validate(record)
        else:
            event = BarEvent.model_validate(record)
        rx = parse_et_iso(rx_text) if rx_text else _event_ts(event)
        out.append(RecordedEvent(rx=rx, event=event))
    return sorted(out, key=lambda r: r.rx)  # sort is stable: equal times keep file order


def _event_ts(event: MarketEvent) -> datetime:
    return event.bar.ts if isinstance(event, BarEvent) else event.ts


class ReplayHub:
    """The market-data hub's interface over a recorded day."""

    def __init__(
        self, *, clock: Clock, events: Sequence[RecordedEvent], symbol_limit: int = 30
    ) -> None:
        self._clock, self._events, self._limit = clock, events, symbol_limit
        self._state = HubState(clock)
        self._symbols: frozenset[str] = frozenset()
        self._channels: tuple[Channel, ...] = TRADING_CHANNELS
        self._task: asyncio.Task[None] | None = None
        self.failed: Exception | None = None
        self.disconnected_since: datetime | None = None
        self.connections = 1

    async def start(self) -> None:
        if self._task is None:
            started = self._clock.now()
            self._task = asyncio.create_task(self._run(started), name="replay-hub")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def wait_connected(self, timeout_s: float) -> bool:
        return True

    async def set_subscriptions(
        self, symbols: Iterable[str], *, channels: Sequence[Channel] | None = None
    ) -> dict[str, list[str]]:
        wanted = frozenset(s.upper() for s in symbols)
        if len(wanted) > self._limit:
            raise SubscriptionLimitError(
                f"{len(wanted)} symbols exceeds the limit of {self._limit}"
            )
        self._symbols = wanted
        if channels is not None:
            self._channels = tuple(channels)
        return self.acked

    @property
    def symbols(self) -> frozenset[str]:
        return self._symbols

    @property
    def acked(self) -> dict[str, list[str]]:
        return {channel: sorted(self._symbols) for channel in self._channels}

    def subscribe_consumer(self) -> asyncio.Queue[MarketEvent]:
        return self._state.subscribe_consumer()

    def remove_consumer(self, queue: asyncio.Queue[MarketEvent]) -> None:
        self._state.remove_consumer(queue)

    def add_tap(self, tap: Tap) -> None:
        self._state.add_tap(tap)

    def last_trade(self, symbol: str) -> Trade | None:
        return self._state.last_trade(symbol)

    def last_quote(self, symbol: str) -> Quote | None:
        return self._state.last_quote(symbol)

    def last_price(self, symbol: str, max_age_s: float = PRICE_MAX_AGE_S) -> Decimal | None:
        return self._state.last_price(symbol, max_age_s)

    def last_event_age_s(self, symbol: str | None = None) -> float:
        return self._state.last_event_age_s(symbol)

    async def _run(self, started: datetime) -> None:
        """Publish by receive time from ``started`` on: like a live hub, a hub started late
        (after a restart) never sees what was received before it existed."""
        for recorded in self._events:
            if recorded.rx < started:
                continue
            if recorded.rx > self._clock.now():
                await self._clock.sleep_until(recorded.rx)
            if recorded.event.symbol in self._symbols and self._carried(recorded.event):
                self._state.publish(recorded.event)

    def _carried(self, event: MarketEvent) -> bool:
        """Whether the subscribed channels carry this kind of event."""
        if isinstance(event, Trade):
            return "trades" in self._channels
        if isinstance(event, Quote):
            return "quotes" in self._channels
        return "bars" in self._channels
