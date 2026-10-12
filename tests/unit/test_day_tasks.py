"""The gate's inputs and the day's background work (P3.5; DECISIONS D-044, D-048)."""

from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.exec_harness import CALENDAR, RISK, SETTINGS, TIMES
from trader.core.clock import ET, SimClock
from trader.core.models import Asset, Quote, Signal, Snapshot, Trade, TriggerEvent
from trader.data.stream import Channel
from trader.data.ws import StreamError
from trader.execution.broker import AccountSnapshot
from trader.execution.service import Exposure
from trader.memory.journal import Journal
from trader.orchestrator.inputs import LiveInputs
from trader.orchestrator.plan import day_plan
from trader.orchestrator.tasks import EquityTrack, StaleWatch, Subscriptions
from trader.risk.breakers import Halts

D = Decimal
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=ET)
ASSET = Asset(
    symbol="XYZ",
    name="X",
    exchange="NASDAQ",
    asset_class="us_equity",
    status="active",
    tradable=True,
)
SIG = Signal(
    id="20261009-XYZ-orb5",
    date=date(2026, 10, 9),
    symbol="XYZ",
    direction="long",
    status="armed",
    rel_vol=2.0,
    rel_vol_rank=1,
    or_open=D("12.00"),
    or_high=D("12.30"),
    or_low=D("11.95"),
    or_close=D("12.25"),
    or_volume=40_000,
    atr14=D("1.00"),
    adv14=2_000_000,
    prev_close=D("11.90"),
    gap_pct=0.8,
    trigger_price=D("12.31"),
    stop_price=D("12.21"),
    max_chase_price=D("12.34"),
    entry_limit=D("12.33"),
)


class Hub:
    def __init__(self, clock: SimClock) -> None:
        self.clock = clock
        self.trade: Trade | None = None
        self.quote: Quote | None = None
        self.age = 1.0
        self.subscribed: list[tuple[list[str], tuple[Channel, ...]]] = []
        self.refuse: list[StreamError] = []

    def last_price(self, symbol: str, max_age_s: float = 60.0) -> Decimal | None:
        return None if self.trade is None else self.trade.price

    def last_trade(self, symbol: str) -> Trade | None:
        return self.trade

    def last_quote(self, symbol: str) -> Quote | None:
        return self.quote

    def last_event_age_s(self, symbol: str | None = None) -> float:
        return self.age

    async def set_subscriptions(
        self, symbols: Iterable[str], *, channels: Sequence[Channel] | None = None
    ) -> dict[str, list[str]]:
        wanted = list(symbols)
        if self.refuse:
            raise self.refuse.pop(0)
        self.subscribed.append((wanted, tuple(channels or ())))
        return {}


class Market:
    def __init__(self) -> None:
        self.snaps: dict[str, Snapshot] = {}
        self.calls = 0
        self.error: Exception | None = None

    async def snapshots(self, symbols: Sequence[str], *, feed: str) -> dict[str, Snapshot]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.snaps


class Broker:
    def __init__(self) -> None:
        self.reads = 0

    async def account(self) -> AccountSnapshot:
        self.reads += 1
        return AccountSnapshot(
            status="ACTIVE",
            cash=D(1000),
            equity=D("1000.50"),
            buying_power=D(1000),
            multiplier=D(1),
            is_paper=True,
        )


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    j = await Journal.open(tmp_path / "trader.db", inline=True)
    yield j
    await j.aclose()


def inputs(
    journal: Journal, clock: SimClock, hub: Hub, market: Market, broker: Broker, kill: Path
) -> LiveInputs:
    return LiveInputs(
        hub=hub,
        market=market,
        brokers={"A": broker},  # type: ignore[dict-item]
        halts=Halts(journal, clock),
        clock=clock,
        times=TIMES,
        mode="paper",
        risk=RISK,
        feed="iex",
        events=(),
        assets={"XYZ": ASSET},
        kill_file=kill,
        hub_stale=lambda: False,
    )


def event(source: Any, price: str = "12.31", ago_s: float = 0.2) -> TriggerEvent:
    return TriggerEvent(
        signal_id=SIG.id,
        symbol="XYZ",
        rank=1,
        ts=NOW - timedelta(seconds=ago_s),
        received_at=NOW,
        price=D(price),
        source=source,
    )


async def test_prepare_reads_the_hub_and_caches_equity(journal: Journal, tmp_path: Path) -> None:
    clock, hub, market, broker = SimClock(NOW), Hub(SimClock(NOW)), Market(), Broker()
    hub.trade = Trade(symbol="XYZ", ts=NOW - timedelta(seconds=2), price=D("12.32"), size=100)
    live = inputs(journal, clock, hub, market, broker, tmp_path / "KILL")
    got = await live.prepare("A", SIG, event("stream_trade"))
    assert (got.last_price, got.quote_age_s, got.broker_equity) == (D("12.32"), 2.0, D("1000.50"))
    assert (got.asset, got.halts, market.calls) == (ASSET, (), 0)
    await clock.advance(timedelta(seconds=5))
    await live.prepare("A", SIG, event("stream_trade"))
    assert broker.reads == 1  # equity is reused for 10 s
    await clock.advance(timedelta(seconds=6))
    await live.prepare("A", SIG, event("stream_trade"))
    assert broker.reads == 2


async def test_an_arm_check_uses_its_own_price_and_time(journal: Journal, tmp_path: Path) -> None:
    clock, hub, market = SimClock(NOW), Hub(SimClock(NOW)), Market()
    live = inputs(journal, clock, hub, market, Broker(), tmp_path / "KILL")
    got = await live.prepare("A", SIG, event("arm_check", "12.33", ago_s=1.5))
    assert (got.last_price, got.quote_age_s, market.calls) == (D("12.33"), 1.5, 0)


async def test_a_stale_quote_is_refreshed_over_rest(journal: Journal, tmp_path: Path) -> None:
    clock, hub, market = SimClock(NOW), Hub(SimClock(NOW)), Market()
    hub.quote = Quote(
        symbol="XYZ",
        ts=NOW - timedelta(seconds=40),
        bid=D("12.30"),
        bid_size=1,
        ask=D("12.32"),
        ask_size=1,
    )
    fresh = Trade(symbol="XYZ", ts=NOW - timedelta(seconds=3), price=D("12.31"), size=100)
    market.snaps = {"XYZ": Snapshot(symbol="XYZ", latest_trade=fresh)}
    live = inputs(journal, clock, hub, market, Broker(), tmp_path / "KILL")
    got = await live.prepare("A", SIG, event("stream_trade"))
    assert (got.quote_age_s, market.calls) == (3.0, 1)
    market.error = RuntimeError("down")
    got = await live.prepare("A", SIG, event("stream_trade"))
    assert got.quote_age_s == 40.0  # the refresh failed: the old age stands, and R-016 rejects


async def test_halts_and_the_kill_file_reach_the_book_and_the_gate(
    journal: Journal, tmp_path: Path
) -> None:
    clock, hub = SimClock(NOW), Hub(SimClock(NOW))
    kill = tmp_path / "KILL"
    live = inputs(journal, clock, hub, Market(), Broker(), kill)
    await Halts(journal, clock).halt("A", "manual", detail="test")
    kill.touch()
    prepared = await live.prepare("A", SIG, event("stream_trade"))
    assert prepared.halts == ("manual", "kill_switch")
    exposure = Exposure(
        symbols=frozenset({"ABC"}),
        entries_today=1,
        symbols_entered_today=frozenset({"ABC"}),
        available_cash=D(500),
        equity_sod=D(1000),
        realized_pnl_today=D(-3),
        open_risk=D(2),
        ledger_equity=D(999),
        unprotected=frozenset(),
        client_order_id_used=False,
        open_order_for_signal=False,
    )
    state = await live.builder("A", SIG, prepared)(exposure)
    assert (state.halts, state.kill_switch, state.available_cash) == (("manual",), True, D(500))
    assert (state.broker_equity, state.asset, state.day) == (D("1000.50"), ASSET, TIMES)


# ---------------------------------------------------------------- subscriptions


async def codes(journal: Journal) -> list[str]:
    return [r["code"] for r in await journal.fetch_all("SELECT code FROM incidents ORDER BY id")]


async def test_subscriptions_keep_held_symbols_and_trim_armed_ones(journal: Journal) -> None:
    hub = Hub(SimClock(NOW))
    subs = Subscriptions(
        hub=hub,
        journal=journal,
        clock=SimClock(NOW),
        limit=5,  # type: ignore[arg-type]
        regime=("SPY", "QQQ"),
    )
    subs.armed = ["AAA", "BBB", "CCC", "HELD"]
    assert await subs.apply({"held"}) == ["HELD", "QQQ", "SPY", "AAA", "BBB"]
    assert subs.dropped == ["CCC"]
    assert await codes(journal) == ["subscription_limited"]


async def test_a_405_drops_quotes_then_the_lowest_ranks(journal: Journal) -> None:
    hub = Hub(SimClock(NOW))
    hub.refuse = [StreamError(405, "limit"), StreamError(405, "limit"), StreamError(405, "limit")]
    subs = Subscriptions(
        hub=hub,
        journal=journal,
        clock=SimClock(NOW),
        limit=30,  # type: ignore[arg-type]
        regime=("SPY",),
    )
    subs.armed = ["AAA", "BBB", "CCC"]
    assert await subs.apply(()) == ["SPY", "AAA"]
    assert (subs.channels, subs.dropped) == (("trades",), ["BBB", "CCC"])
    hub.refuse = [StreamError(400, "bad")]
    assert await subs.apply(()) == []
    assert await codes(journal) == ["subscription_limited"]  # one P2 a day


# ---------------------------------------------------------------- staleness and equity


async def test_stale_data_is_one_p2_per_episode(journal: Journal) -> None:
    open_ = datetime(2026, 10, 9, 9, 30, tzinfo=ET)
    clock = SimClock(open_ - timedelta(minutes=1))
    hub = Hub(clock)
    plan = day_plan(CALENDAR, date(2026, 10, 9), SETTINGS, RISK)
    watch = StaleWatch(
        hub=hub,
        clock=clock,
        journal=journal,
        plan=plan,  # type: ignore[arg-type]
        warn_s=30,
        halt_s=120,
    )
    hub.age = float("inf")
    await watch.check()
    assert not watch.stale  # nothing subscribed yet
    watch.started()
    await clock.advance_to(open_ + timedelta(seconds=60))
    await watch.check()
    assert not watch.stale  # quiet for 60 s since the open: only a warning
    await clock.advance_to(open_ + timedelta(seconds=130))
    await watch.check()
    await watch.check()
    assert watch.stale
    hub.age = 1.0
    await watch.check()
    assert not watch.stale
    assert await codes(journal) == ["data_stale"]


def test_equity_track_measures_peak_to_trough() -> None:
    track = EquityTrack(peak=D(1000))
    for marked in ("1010", "995", "1020", "1004"):
        track.sample(D(marked))
    assert (track.peak, track.max_drawdown) == (D(1020), D(16))
