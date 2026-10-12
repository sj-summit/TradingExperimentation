"""Recording and replaying a day (P3.8a; DECISIONS D-047)."""

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trader.core.clock import ET, SimClock
from trader.core.models import Asset, Bar, OrderIntent, Quote, Snapshot, Trade
from trader.data.alpaca_rest import SipRecentError
from trader.data.recorder import STREAM_FILE, Recorder, broker_file, read_jsonl
from trader.data.replay import (
    RecordedEvent,
    RecordingMarketData,
    ReplayHub,
    ReplayMarketData,
    ReplayMissError,
    load_stream,
)
from trader.data.stream import SubscriptionLimitError
from trader.execution.broker import OrderRejected
from trader.execution.recording import RecordingBroker
from trader.execution.sim_broker import SimBroker
from trader.memory.journal import Journal
from trader.orchestrator.simdrive import StuckError, drive

D = Decimal
T0 = datetime(2026, 10, 13, 9, 35, 3, tzinfo=ET)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class Inner:
    """A REST client whose calls take simulated time."""

    def __init__(self, clock: SimClock) -> None:
        self.clock = clock

    async def _take(self, seconds: float) -> None:
        await self.clock.sleep_until(self.clock.now() + timedelta(seconds=seconds))

    async def calendar(self, start: date, end: date) -> list[dict[str, str]]:
        await self._take(0.1)
        return [{"date": "2026-10-13", "open": "09:30", "close": "16:00"}]

    async def assets(self, *, status: str | None = "active") -> list[Asset]:
        await self._take(0.2)
        return [Asset(symbol="XYZ", name="X", exchange="NYSE", asset_class="us_equity",
                      status="active", tradable=True)]  # fmt: skip

    async def asset(self, symbol: str) -> Asset | None:
        await self._take(0.05)
        return None

    async def bars(self, symbols: Any, timeframe: str, start: datetime, end: Any = None, *,
                   feed: str, adjustment: str = "raw") -> dict[str, list[Bar]]:  # fmt: skip
        await self._take(1.5)
        if timeframe == "1Min":
            raise SipRecentError(403, "subscription does not permit querying recent SIP data", "/b")
        return {"XYZ": [Bar(ts=start, open=D("12.40"), high=D("12.71"), low=D("12.30"),
                            close=D("12.65"), volume=50_000)]}  # fmt: skip

    async def snapshots(self, symbols: Any, *, feed: str) -> dict[str, Snapshot]:
        await self._take(0.3)
        trade = Trade(symbol="XYZ", ts=T0, price=D("12.74"), size=100)
        return {"XYZ": Snapshot(symbol="XYZ", latest_trade=trade)}

    async def splits(self, start: date, end: date) -> list[Any]:
        await self._take(0.1)
        return []

    async def aclose(self) -> None:
        return None


async def test_rest_calls_replay_with_their_results_and_durations() -> None:
    clock = SimClock(T0)
    sink: list[dict[str, Any]] = []
    rec = RecordingMarketData(Inner(clock), clock, sink.append)  # type: ignore[arg-type]

    async def day() -> list[object]:
        out: list[object] = [await rec.bars(["XYZ"], "5Min", at(-303), at(-4), feed="iex",
                                            adjustment="split")]  # fmt: skip
        out.append(await rec.snapshots(["XYZ"], feed="iex"))
        out.append(await rec.assets(status=None))
        out.append(await rec.snapshots(["XYZ"], feed="iex"))
        with pytest.raises(SipRecentError):
            await rec.bars(["XYZ"], "1Min", at(-303), feed="sip")
        return out

    live = await drive(clock, day())
    assert [r["method"] for r in sink] == ["bars", "snapshots", "assets", "snapshots", "bars"]
    assert sink[-1]["error"]["type"] == "SipRecentError"
    replay_clock = SimClock(T0 + timedelta(hours=1))  # replay may start at any time
    replay = ReplayMarketData(sink, replay_clock)

    async def again() -> list[object]:
        started = replay_clock.now()
        out: list[object] = [await replay.bars(["XYZ"], "5Min", at(-303), at(-4), feed="iex",
                                               adjustment="split")]  # fmt: skip
        assert replay_clock.now() - started == timedelta(seconds=1.5)  # the recorded duration
        out.append(await replay.snapshots(["XYZ"], feed="iex"))
        out.append(await replay.assets(status=None))
        out.append(await replay.snapshots(["XYZ"], feed="iex"))
        with pytest.raises(SipRecentError):
            await replay.bars(["XYZ"], "1Min", at(-303), feed="sip")
        return out

    assert await drive(replay_clock, again()) == live
    assert replay.divergences == []
    await drive(replay_clock, replay.snapshots(["XYZ"], feed="iex"))  # a third, unrecorded call
    assert len(replay.divergences) == 1
    with pytest.raises(ReplayMissError):
        await drive(replay_clock, replay.snapshots(["ABC"], feed="iex"))


def event(kind: str, symbol: str, seconds: float, price: str = "12.70",
          cond: tuple[str, ...] = ("@",)) -> RecordedEvent:  # fmt: skip
    ts = at(seconds)
    ev: Any = (
        Trade(symbol=symbol, ts=ts, price=D(price), size=100, conditions=cond)
        if kind == "t"
        else Quote(symbol=symbol, ts=ts, bid=D("12.69"), bid_size=1, ask=D("12.71"), ask_size=1)
    )
    return RecordedEvent(rx=ts + timedelta(milliseconds=20), event=ev)


async def test_the_replay_hub_publishes_by_receive_time_to_subscribers() -> None:
    clock = SimClock(T0)
    events = [event("t", "XYZ", 1), event("q", "ABC", 2), event("t", "XYZ", 3, "12.80", ("I",)),
              event("t", "XYZ", 4, "12.75")]  # fmt: skip
    hub = ReplayHub(clock=clock, events=events, symbol_limit=2)
    with pytest.raises(SubscriptionLimitError):
        await hub.set_subscriptions(["A", "B", "C"])
    assert await hub.set_subscriptions(["xyz"]) == {"trades": ["XYZ"], "quotes": ["XYZ"]}
    tapped: list[tuple[datetime, Decimal]] = []
    hub.add_tap(
        lambda ev: tapped.append((clock.now(), ev.price)) if isinstance(ev, Trade) else None
    )
    queue = hub.subscribe_consumer()
    gone = hub.subscribe_consumer()
    hub.remove_consumer(gone)
    await hub.start()
    await drive(clock, _until(clock, at(5)))
    await hub.stop()
    assert [(round((t - T0).total_seconds(), 2), p) for t, p in tapped] == [
        (1.02, D("12.70")), (3.02, D("12.80")), (4.02, D("12.75")),
    ]  # fmt: skip
    assert queue.qsize() == 3  # ABC wasn't subscribed
    assert gone.qsize() == 0
    assert hub.last_price("XYZ") == D("12.75")  # the odd lot (I) never set the price
    assert hub.last_trade("XYZ") is not None
    await drive(clock, _until(clock, at(70)))
    assert hub.last_price("XYZ") is None  # older than 60 s: no price at all


async def _until(clock: SimClock, when: datetime) -> None:
    await clock.sleep_until(when)


def test_load_stream_orders_by_receive_time(tmp_path: Path) -> None:
    rec = Recorder(tmp_path, date(2026, 10, 13))
    late = Trade(symbol="XYZ", ts=at(1), price=D("12.70"), size=1)
    early = Trade(symbol="XYZ", ts=at(2), price=D("12.71"), size=1)
    rec.record_event(late, rx=at(5))  # stamped earlier by the exchange, received later
    rec.record_event(early, rx=at(3))
    rec.close()
    loaded = load_stream(rec.dir / STREAM_FILE)
    assert [r.event.price for r in loaded] == [D("12.71"), D("12.70")]  # type: ignore[union-attr]


async def test_the_recording_broker_writes_calls_errors_and_updates() -> None:
    clock = SimClock(T0)
    sim = SimBroker(clock=clock)
    sim.feed(Quote(symbol="XYZ", ts=T0, bid=D("12.30"), bid_size=5, ask=D("12.32"), ask_size=5))
    sink: list[dict[str, Any]] = []
    broker = RecordingBroker(sim, clock, sink.append)
    updates = broker.trade_updates()
    first = await updates.__anext__()  # SimBroker opens with a resync
    intent = OrderIntent(book="A", signal_id="s", symbol="XYZ", qty=40, entry_limit=D("12.35"),
                         stop_price=D("12.21"), client_order_id="A-20261013-XYZ-entry-1",
                         day_size_multiplier=D(1))  # fmt: skip
    order = await broker.submit_entry_oto(intent)
    bad = {"client_order_id": "bad", "stop_price": D("12.40")}  # stop above the limit
    with pytest.raises(OrderRejected):
        await broker.submit_entry_oto(intent.model_copy(update=bad))
    await broker.positions()
    calls = [r for r in sink if r["method"] != "trade_update"]
    assert [r["method"] for r in calls] == ["submit_entry_oto", "submit_entry_oto", "positions"]
    assert calls[0]["args"]["client_order_id"] == "A-20261013-XYZ-entry-1"
    assert calls[0]["result"]["id"] == order.id
    assert "raw" not in calls[0]["result"]
    assert all("raw" not in leg for leg in calls[0]["result"]["legs"])
    assert calls[1]["error"]["type"] == "OrderRejected"
    assert sink[0]["event"]["type"] == "resync"
    assert first is not None


async def test_the_inline_journal_needs_no_threads(tmp_path: Path) -> None:
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    assert journal.idle
    await journal.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    assert await journal.get_meta("k") == "v"
    await journal.backup(tmp_path / "copy" / "trader.db")
    await journal.aclose()
    copy = await Journal.open(tmp_path / "copy" / "trader.db", inline=True)
    assert await copy.get_meta("k") == "v"
    await copy.aclose()


async def test_drive_runs_a_job_on_simulated_time_and_refuses_a_stuck_one() -> None:
    clock = SimClock(T0)

    async def job() -> datetime:
        await clock.sleep_until(at(3600))
        await asyncio.sleep(0)
        return clock.now()

    assert await drive(clock, job()) == at(3600)
    never = asyncio.Event()
    with pytest.raises(StuckError):
        await drive(clock, never.wait())


def test_broker_recordings_are_one_file_per_book(tmp_path: Path) -> None:
    rec = Recorder(tmp_path, date(2026, 10, 13))
    rec.append(broker_file("A"), {"method": "positions", "result": []})
    rec.append(broker_file("B"), {"method": "account"})
    rec.close()
    assert broker_file("A") == "broker-A.jsonl.gz"
    assert list(read_jsonl(rec.dir / broker_file("A"))) == [{"method": "positions", "result": []}]
