"""The post-close shadow job and the boot regime job (P3.6b; DECISIONS D-045)."""

from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.fixtures.exec_harness import CALENDAR, RISK, SETTINGS
from tests.fixtures.simtime import JumpClock
from trader.core.calendar import day_times
from trader.core.clock import ET
from trader.core.models import Bar, Signal
from trader.data.alpaca_rest import SipRecentError
from trader.memory.journal import Journal
from trader.memory.regime import ensure_regime, load_regime
from trader.memory.shadow import ShadowParams, compute_shadow
from trader.strategy.orb_sip import save_signals

D = Decimal
DAY = date(2026, 10, 13)
TIMES = day_times(CALENDAR.session(DAY), entry_window_start=RISK.entry_window_start,
                  last_entry=SETTINGS.schedule.last_entry,
                  flatten=SETTINGS.schedule.flatten)  # fmt: skip
PARAMS = ShadowParams(
    SETTINGS.strategy.shadow.slippage_fixed, SETTINGS.strategy.shadow.slippage_pct
)


def at(hh: int, mm: int) -> datetime:
    return datetime.combine(DAY, time(hh, mm), ET)


def m(hh: int, mm: int, o: str, h: str, low: str, c: str) -> Bar:
    return Bar(ts=at(hh, mm), open=D(o), high=D(h), low=D(low), close=D(c), volume=1_000)


def sig(symbol: str, rank: int, direction: str = "long", live: bool = False) -> Signal:
    long = direction == "long"
    priced = direction != "none"
    return Signal(
        id=f"20261013-{symbol}-orb5", date=DAY, symbol=symbol, direction=direction,  # type: ignore[arg-type]
        status="triggered" if live else "armed" if long else "not_armed", rel_vol=5.0,
        rel_vol_rank=rank, or_open=D("10"), or_high=D("10"), or_low=D("9.8"), or_close=D("10"),
        or_volume=10_000, atr14=D("1"), adv14=2_000_000, prev_close=D("9.5"), gap_pct=5.0,
        trigger_price=(D("10.00") if long else D("9.50")) if priced else None,
        stop_price=(D("9.90") if long else D("9.60")) if priced else None,
        triggered_at=at(9, 40) if live else None,
    )  # fmt: skip


WINNER = [m(9, 40, "9.98", "10.05", "9.97", "10.04"), m(15, 55, "10.50", "10.5", "10.5", "10.5")]
QUIET = [m(9, 40, "9.95", "9.99", "9.92", "9.95"), m(15, 55, "9.97", "9.97", "9.97", "9.97")]
DROP = [m(9, 41, "9.52", "9.55", "9.45", "9.48"), m(15, 55, "9.30", "9.3", "9.3", "9.3")]


class Minutes:
    def __init__(self, bars: dict[str, list[Bar]], *, fail: int = 0) -> None:
        self.by_symbol, self.fail = bars, fail
        self.calls: list[tuple[str, ...]] = []

    async def bars(self, symbols: Sequence[str], timeframe: str, start: datetime,
                   end: datetime | None = None, *, feed: str, adjustment: str = "raw",
                   ) -> dict[str, list[Bar]]:  # fmt: skip
        self.calls.append(tuple(symbols))
        if self.fail:
            self.fail -= 1
            raise SipRecentError(403, "subscription does not permit querying recent SIP data",
                                 "/v2/stocks/bars")  # fmt: skip
        assert (timeframe, start, end, feed) == ("1Min", at(9, 30), None, "sip")
        return {s: self.by_symbol[s] for s in symbols if s in self.by_symbol}


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    yield opened
    await opened.aclose()


SIGNALS = [sig("LIVE", 1, live=True), sig("SIPO", 2), sig("NONE", 3),
           sig("SHRT", 4, "shadow_short"),
           sig("DOJI", 5, "none")]  # fmt: skip
MARKET = {"LIVE": WINNER, "SIPO": WINNER, "NONE": QUIET, "SHRT": DROP}


async def run(journal: Journal, clock: JumpClock, market: Minutes) -> object:
    return await compute_shadow(journal=journal, market=market, clock=clock, calendar=CALENDAR,
                                day=DAY, times=TIMES, or_minutes=5, params=PARAMS,
                                signals=SIGNALS)  # fmt: skip


async def stored(journal: Journal) -> dict[str, tuple[str, float | None, int | None]]:
    rows = await journal.fetch_all(
        "SELECT signal_id, exit_reason, shadow_r, sip_only_trigger, path_source "
        "FROM shadow_outcomes"
    )
    assert {r["path_source"] for r in rows} <= {"sip_1min"}
    return {r["signal_id"][9:-5]: (r["exit_reason"], r["shadow_r"], r["sip_only_trigger"])
            for r in rows}  # fmt: skip


async def test_outcomes_for_every_priced_signal(journal: Journal) -> None:
    await save_signals(journal, SIGNALS)
    clock = JumpClock(at(16, 12))
    market = Minutes(MARKET)
    result = await run(journal, clock, market)
    assert result.ok  # type: ignore[attr-defined]
    assert market.calls == [("LIVE", "NONE", "SHRT", "SIPO")]  # one request; the doji has no prices
    rows = await stored(journal)
    assert set(rows) == {"LIVE", "SIPO", "NONE", "SHRT"}
    assert rows["LIVE"][0] == "flatten"
    assert rows["LIVE"][2] == 0  # the live feed triggered it too
    assert rows["SIPO"][2] == 1  # only the SIP bars reached the trigger
    assert rows["NONE"] == ("no_trigger", None, 0)
    assert rows["SHRT"][0] == "flatten"
    assert rows["SHRT"][1] is not None and rows["SHRT"][1] > 0  # noqa: PT018
    assert rows["SHRT"][2] is None  # shorts are never armed
    await run(journal, clock, Minutes(MARKET))  # a rerun replaces, never duplicates
    assert len(await stored(journal)) == 4


async def test_it_waits_for_the_flatten_minute_to_age_into_the_sip_window(journal: Journal) -> None:
    await save_signals(journal, SIGNALS)
    clock = JumpClock(at(15, 58))
    await run(journal, clock, Minutes(MARKET))
    assert clock.now() >= at(16, 11)  # 15:55 + 16 min


async def test_too_recent_data_is_retried_then_given_up(journal: Journal) -> None:
    await save_signals(journal, SIGNALS)
    clock = JumpClock(at(16, 12))
    flaky = Minutes(MARKET, fail=2)
    assert (await run(journal, clock, flaky)).ok  # type: ignore[attr-defined]
    assert len(flaky.calls) == 3
    assert clock.now() == at(16, 13)  # two 30 s retries
    stuck = Minutes(MARKET, fail=100)
    result = await run(journal, JumpClock(at(16, 30)), stuck)
    assert not result.ok  # type: ignore[attr-defined]
    assert "recent SIP" in result.error  # type: ignore[attr-defined]


class Daily:
    def __init__(self, bars: list[Bar] | None) -> None:
        self.data, self.calls = bars, 0

    async def bars(self, symbols: Sequence[str], timeframe: str, start: datetime,
                   end: datetime | None = None, *, feed: str, adjustment: str = "raw",
                   ) -> dict[str, list[Bar]]:  # fmt: skip
        self.calls += 1
        assert (timeframe, feed, adjustment, end) == ("1Day", "sip", "split", None)
        if self.data is None:
            raise SipRecentError(403, "forbidden", "/v2/stocks/bars")
        return {"SPY": self.data}


def spy_year(last: date) -> list[Bar]:
    days = [last - timedelta(days=i) for i in range(299, -1, -1)]
    return [Bar(ts=datetime.combine(d, time(0), ET), open=D(500), high=D(502), low=D(498),
                close=D(500), volume=1) for d in days]  # fmt: skip


async def test_the_regime_is_computed_once_and_stored(journal: Journal) -> None:
    prev = date(2026, 10, 12)
    market = Daily(spy_year(prev))
    tags = await ensure_regime(journal=journal, market=market, clock=JumpClock(at(8, 30)), d=prev)
    assert tags is not None
    assert (tags.spy_trend, tags.vol_bucket) == ("down", "mid")
    again = await ensure_regime(journal=journal, market=market, clock=JumpClock(at(8, 31)), d=prev)
    assert again == tags
    assert market.calls == 1
    assert await load_regime(journal, prev) == tags


async def test_an_unavailable_regime_is_none_not_wrong(journal: Journal) -> None:
    clock = JumpClock(at(8, 30))
    assert await ensure_regime(journal=journal, market=Daily(None), clock=clock, d=DAY) is None
    short_history = Daily(spy_year(DAY)[-30:])
    assert await ensure_regime(journal=journal, market=short_history, clock=clock, d=DAY) is None
    assert await load_regime(journal, DAY) is None
