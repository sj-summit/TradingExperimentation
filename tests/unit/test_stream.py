import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fixtures import nyse
from tests.fixtures.fake_ws import FakeAlpacaServer
from trader import runtime
from trader.cli import app
from trader.core.clock import ET, Clock, SimClock
from trader.core.models import Bar, Quote, Trade
from trader.data import ws
from trader.data.stream import (
    Backfill,
    BarEvent,
    MarketDataHub,
    MarketEvent,
    StreamFeed,
    SubscriptionLimitError,
)
from trader.data.ws import StreamError

NOW = datetime(2026, 10, 9, 10, 0, tzinfo=ET)


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await asyncio.sleep(0)


def make_hub(
    server: FakeAlpacaServer,
    sleeps: Sleeps,
    *,
    feed: StreamFeed = "iex",
    clock: Clock | None = None,
    limit: int = 30,
    backfill: Backfill | None = None,
    market_open: Callable[[datetime], bool] = lambda _now: False,
    silence_s: float = 60.0,
) -> MarketDataHub:
    return MarketDataHub(
        key_id="PKTEST",
        secret_key="secret-value",
        feed=feed,
        clock=clock or SimClock(NOW),
        symbol_limit=limit,
        backfill=backfill,
        market_open=market_open,
        connector=server.connect,
        silence_s=silence_s,
        sleep=sleeps,
    )


def change(action: str, *symbols: str) -> dict[str, Any]:
    """A subscribe/unsubscribe message for trades, quotes and bars."""
    listed = sorted(symbols)
    return {"action": action, "trades": listed, "quotes": listed, "bars": listed}


def trade_msg(price: float, ts: str) -> dict[str, Any]:
    return {"T": "t", "S": "SPY", "i": 7, "x": "V", "p": price, "s": 100, "c": ["@"], "t": ts}


def quote_msg(bid: float, ask: float, ts: str) -> dict[str, Any]:
    return {"T": "q", "S": "SPY", "bp": bid, "bs": 2, "ap": ask, "as": 3, "c": ["R"], "t": ts}


def bar_msg(symbol: str, minute: int, kind: str = "b", close: float = 571.25) -> dict[str, Any]:
    ts = (NOW + timedelta(minutes=minute)).isoformat()
    return {"T": kind, "S": symbol, "o": 571.0, "h": 571.5, "l": 570.9, "c": close, "v": 1200,
            "n": 14, "vw": 571.2, "t": ts}  # fmt: skip


def bar_at(minute: int) -> Bar:
    px = Decimal("571.00")
    return Bar(ts=NOW + timedelta(minutes=minute), open=px, high=px, low=px, close=px, volume=10)


async def until(condition: Callable[[], bool], timeout_s: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.01)


async def take(queue: "asyncio.Queue[MarketEvent]", n: int) -> list[MarketEvent]:
    return [await asyncio.wait_for(queue.get(), 1.0) for _ in range(n)]


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def server() -> FakeAlpacaServer:
    return FakeAlpacaServer()


@pytest.fixture
async def hub(server: FakeAlpacaServer, sleeps: Sleeps) -> AsyncIterator[MarketDataHub]:
    hub = make_hub(server, sleeps)
    await hub.start()
    assert await hub.wait_connected(1.0)
    yield hub
    await hub.stop()


# ---------------------------------------------------------------- handshake and subscriptions


async def test_handshake_then_subscribe(hub: MarketDataHub, server: FakeAlpacaServer) -> None:
    acked = await hub.set_subscriptions(["spy", "QQQ"])
    conn = server.connections[0]
    assert conn.url == "wss://stream.data.alpaca.markets/v2/iex"
    assert conn.sent[0] == {"action": "auth", "key": "PKTEST", "secret": "secret-value"}
    assert conn.sent[1] == change("subscribe", "QQQ", "SPY")
    pair = ["QQQ", "SPY"]
    assert acked == {"trades": pair, "quotes": pair, "bars": pair}  # "corrections" isn't ours


async def test_unsubscribe_goes_before_subscribe(
    hub: MarketDataHub, server: FakeAlpacaServer
) -> None:
    await hub.set_subscriptions(["AAA", "BBB"])
    await hub.set_subscriptions(["BBB", "CCC"])
    unsubscribe, subscribe = server.connections[0].sent[-2:]
    assert unsubscribe == change("unsubscribe", "AAA")
    assert subscribe == change("subscribe", "CCC")
    assert hub.acked["trades"] == ["BBB", "CCC"]


async def test_local_symbol_limit(server: FakeAlpacaServer, sleeps: Sleeps) -> None:
    hub = make_hub(server, sleeps, limit=2)
    with pytest.raises(SubscriptionLimitError):
        await hub.set_subscriptions(["A", "B", "C"])
    assert server.connections == []  # never asked the server


async def test_server_405_keeps_the_previous_set(
    hub: MarketDataHub, server: FakeAlpacaServer
) -> None:
    server.symbol_limit = 2
    await hub.set_subscriptions(["AAA", "BBB"])
    with pytest.raises(StreamError) as info:
        await hub.set_subscriptions(["AAA", "BBB", "CCC"])
    assert info.value.code == 405
    assert hub.acked["trades"] == ["AAA", "BBB"]
    assert server.connections[0].subscriptions["trades"] == {"AAA", "BBB"}


async def test_subscriptions_made_before_connecting_apply_on_connect(
    server: FakeAlpacaServer, sleeps: Sleeps
) -> None:
    hub = make_hub(server, sleeps, feed="test")
    assert await hub.set_subscriptions(["FAKEPACA"]) == {}
    await hub.start()
    await until(lambda: len(server.connections) == 1 and len(server.connections[0].sent) >= 2)
    conn = server.connections[0]
    assert conn.url.endswith("/v2/test")
    assert conn.sent[1] == change("subscribe", "FAKEPACA")
    await hub.stop()


# ---------------------------------------------------------------- events


async def test_events_are_parsed_and_fanned_out(
    hub: MarketDataHub, server: FakeAlpacaServer
) -> None:
    await hub.set_subscriptions(["SPY"])
    first, second = hub.subscribe_consumer(), hub.subscribe_consumer()
    server.connections[0].push(
        trade_msg(571.12, "2026-10-09T14:00:01.123456789Z"),
        quote_msg(571.1, 571.14, "2026-10-09T14:00:01Z"),
        bar_msg("SPY", 0),
        bar_msg("SPY", 0, kind="u", close=571.30),
        {"T": "s", "S": "SPY", "sc": "H", "sm": "Trading Halt", "t": "2026-10-09T14:00:02Z"},
    )
    for queue in (first, second):
        trade, quote, bar, updated = await take(queue, 4)
        assert isinstance(trade, Trade)
        assert trade.price == Decimal("571.12")
        assert trade.ts.tzinfo is ET
        assert trade.ts.hour == 10
        assert isinstance(quote, Quote)
        assert (quote.bid, quote.ask) == (Decimal("571.1"), Decimal("571.14"))
        assert isinstance(bar, BarEvent)
        assert bar.source == "stream"
        assert isinstance(updated, BarEvent)
        assert updated.source == "updated"
        assert updated.bar.close == Decimal("571.3")
        assert queue.empty()  # the status message isn't an event yet
    assert hub.last_trade("SPY") is not None
    assert hub.last_quote("SPY") is not None
    assert hub.last_event_age_s("SPY") == 0.0
    assert hub.last_event_age_s("QQQ") == float("inf")


# ---------------------------------------------------------------- failures and reconnects


async def test_drop_reconnects_resubscribes_and_backfills(
    server: FakeAlpacaServer, sleeps: Sleeps
) -> None:
    clock = SimClock(NOW)
    asked: list[tuple[tuple[str, ...], datetime]] = []

    async def backfill(
        symbols: Sequence[str], start: datetime, end: datetime
    ) -> Mapping[str, Sequence[Bar]]:
        asked.append((tuple(symbols), start))
        return {"SPY": [bar_at(0), bar_at(1), bar_at(2)]}

    hub = make_hub(server, sleeps, clock=clock, backfill=backfill)
    events = hub.subscribe_consumer()
    await hub.start()
    await hub.wait_connected(1.0)
    await hub.set_subscriptions(["SPY"])
    server.connections[0].push(bar_msg("SPY", 0))
    await take(events, 1)
    await clock.advance(timedelta(minutes=3))
    server.connections[0].drop()
    await until(lambda: hub.connections == 2)
    assert server.connections[1].sent[1] == change("subscribe", "SPY")
    filled = await take(events, 2)
    assert [e.bar.ts.minute for e in filled if isinstance(e, BarEvent)] == [1, 2]
    assert all(isinstance(e, BarEvent) and e.source == "backfill" for e in filled)
    assert asked == [(("SPY",), NOW + timedelta(minutes=2))]  # a minute before the drop
    assert hub.disconnected_since is None
    await hub.stop()


async def test_bad_keys_stop_the_stream(sleeps: Sleeps) -> None:
    server = FakeAlpacaServer(auth_errors=[(402, "auth failed")])
    hub = make_hub(server, sleeps)
    await hub.start()
    assert not await hub.wait_connected(1.0)
    assert isinstance(hub.failed, StreamError)
    assert hub.failed.code == 402
    await asyncio.sleep(0.05)
    assert len(server.connections) == 1  # no retries
    await hub.stop()


async def test_connection_limit_backs_off_before_retrying(sleeps: Sleeps) -> None:
    server = FakeAlpacaServer(auth_errors=[(406, "connection limit exceeded")])
    hub = make_hub(server, sleeps)
    await hub.start()
    assert await hub.wait_connected(1.0)
    assert len(server.connections) == 2
    assert sleeps.calls == [30.0]  # Alpaca clears stale sessions slowly
    await hub.stop()


@pytest.mark.parametrize(("market_open", "reconnects"), [(True, True), (False, False)])
async def test_silence_watchdog(sleeps: Sleeps, market_open: bool, reconnects: bool) -> None:
    server = FakeAlpacaServer()
    hub = make_hub(server, sleeps, market_open=lambda _now: market_open, silence_s=0.05)
    await hub.start()
    await hub.wait_connected(1.0)
    await hub.set_subscriptions(["SPY"])
    await asyncio.sleep(0.3)  # no data at all
    assert (len(server.connections) > 1) is reconnects
    await hub.stop()


async def test_stop_closes_the_connection(hub: MarketDataHub, server: FakeAlpacaServer) -> None:
    await hub.stop()
    assert server.connections[0].closed


# ---------------------------------------------------------------- trader stream


class FakeRest:
    """Just enough of AlpacaRest for `trader stream`."""

    async def calendar(self, start: date, end: date) -> list[dict[str, str]]:
        return nyse.calendar_rows(max(start, nyse.FIRST), min(end, nyse.LAST))

    async def __aenter__(self) -> "FakeRest":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def test_cli_stream_on_the_test_feed(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeAlpacaServer(after_subscribe=lambda conn: conn.push(bar_msg("FAKEPACA", 0)))
    monkeypatch.setattr(ws, "websockets_connector", server.connect)
    monkeypatch.setattr(runtime.AlpacaRest, "from_config", lambda _cfg, _clock: FakeRest())
    args = ["--root", str(repo), "stream", "--feed", "test", "--minutes", "0.01"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert server.connections[0].url.endswith("/v2/test")
    assert "subscribed: bars FAKEPACA; quotes FAKEPACA; trades FAKEPACA" in result.output
    assert "10:00 FAKEPACA" in result.output
    assert "done: 1 bars, 0 trades, 1 connection(s)" in result.output
