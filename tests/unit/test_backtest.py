"""The rules-only backtest (P3.9; DECISIONS D-042) on a synthetic history, offline."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from tests.fixtures import nyse
from tests.fixtures.exec_harness import CALENDAR, RISK, SETTINGS
from tests.fixtures.simtime import JumpClock
from trader import runtime
from trader.backtest import report
from trader.backtest.data import BacktestData, LookAheadError
from trader.backtest.engine import BacktestResult, BacktestSettings, busy_hours, run_backtest
from trader.cli import app
from trader.core.clock import ET
from trader.core.models import Asset, Bar, Split
from trader.data.alpaca_rest import AlpacaError
from trader.memory.journal import Journal

D = Decimal
SESSIONS = [s.date for s in CALENDAR.sessions_between(date(2026, 9, 1), date(2026, 10, 30))]
ONE = D("1.0")
FIRST_DAY = date(2026, 10, 5)  # bars before it are lookback; in-range quiet days print less


def at(day: date, hh: int, mm: int) -> datetime:
    return datetime.combine(day, time(hh, mm), ET)


def bar(ts: datetime, o: Decimal, h: Decimal, low: Decimal, c: Decimal, v: int = 1_000) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=low, close=c, volume=v)


@dataclass
class Stock:
    symbol: str
    level: Decimal  # the constant daily close
    daily_range: Decimal = ONE  # ATR14 equals it
    adv: int = 2_000_000
    status: str = "active"
    exchange: str = "NYSE"


class History:
    """Raw history as Alpaca would serve it with ``adjustment=raw`` and ``asof=-``."""

    def __init__(self, stocks: Sequence[Stock]) -> None:
        self.stocks = {s.symbol: s for s in stocks}
        self.daily: dict[str, list[Bar]] = {}
        for s in stocks:
            half = s.daily_range / 2
            self.daily[s.symbol] = [
                bar(at(d, 0, 0), s.level, s.level + half, s.level - half, s.level, s.adv)
                for d in SESSIONS
            ]
        self.or_bars: dict[tuple[str, date], Bar] = {}
        for s in stocks:
            for d in SESSIONS:
                volume = 3_000 if d < FIRST_DAY else 2_000  # in range: rel_vol < 1 unless hot
                self.or_bars[(s.symbol, d)] = bar(at(d, 9, 30), s.level, s.level + D("0.1"),
                                                  s.level - D("0.1"), s.level, volume)  # fmt: skip
        self.minute: dict[tuple[str, date], list[Bar]] = {}
        self.split_list: list[Split] = []
        self.requests: list[str] = []

    def hot(self, symbol: str, day: date, *, minute: int, wins: bool) -> None:
        """A bullish opening range at 10x the baseline volume; the trigger prints at
        09:40 + ``minute``; a winner runs to the flatten, a loser stops out at 09:50."""
        c = self.stocks[symbol].level
        self.or_bars[(symbol, day)] = bar(at(day, 9, 30), c, c + D("0.40"), c - D("0.10"),
                                          c + D("0.35"), 30_000)  # fmt: skip
        trig = bar(at(day, 9, 40 + minute), c + D("0.38"), c + D("0.45"), c + D("0.36"),
                   c + D("0.44"))  # fmt: skip
        path = [trig]
        if wins:
            path.append(bar(at(day, 12, 0), c + D("0.7"), c + D("0.9"), c + D("0.6"), c + D("0.8")))
            path.append(
                bar(at(day, 15, 55), c + D("1.0"), c + D("1.0"), c + D("1.0"), c + D("1.0"))
            )
        else:
            path.append(bar(at(day, 9, 50), c + D("0.35"), c + D("0.36"), c + D("0.20"),
                            c + D("0.22")))  # fmt: skip
        self.minute[(symbol, day)] = path

    async def assets(self, *, status: str | None = "active") -> list[Asset]:
        self.requests.append("assets")
        assert status is None  # the backtest wants delisted symbols too
        return [Asset(symbol=s.symbol, name=s.symbol, exchange=s.exchange, asset_class="us_equity",
                      status=s.status, tradable=s.status == "active")
                for s in self.stocks.values()]  # fmt: skip

    async def bars(self, symbols: Sequence[str], timeframe: str, start: datetime,
                   end: datetime | None = None, *, feed: str, adjustment: str = "raw",
                   asof: str | None = None) -> dict[str, list[Bar]]:  # fmt: skip
        self.requests.append(timeframe)
        assert (adjustment, asof) == ("raw", "-")
        if timeframe == "1Day":
            assert end is not None
            return {s: [b for b in self.daily[s] if start <= b.ts <= end]
                    for s in symbols if s in self.daily}  # fmt: skip
        day = start.date()
        if timeframe == "5Min":
            return {s: [self.or_bars[(s, day)]] for s in symbols if (s, day) in self.or_bars}
        return {s: self.minute[(s, day)] for s in symbols if (s, day) in self.minute}

    async def splits(self, start: date, end: date) -> list[Split]:
        self.requests.append("splits")
        return [s for s in self.split_list if start <= s.ex_date <= end]


@pytest.fixture
async def cache(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "backtest" / "cache.db")
    yield opened
    await opened.aclose()


async def backtest(history: History, cache: Journal, start: date, end: date) -> BacktestResult:
    data = BacktestData(source=history, cache=cache, calendar=CALENDAR)
    return await run_backtest(data, calendar=CALENDAR, settings=SETTINGS, risk=RISK,
                              cfg=BacktestSettings(start=start, end=end, feed="iex"))  # fmt: skip


def day_of(result: BacktestResult, d: date) -> dict[str, tuple[bool, str | None]]:
    """symbol -> (traded, skip) for the day's signals."""
    day = next(x for x in result.days if x.day == d)
    return {r.signal.symbol: (r.trade is not None, r.skip) for r in day.signals}


def stocks() -> list[Stock]:
    return [
        Stock("GOOD", D("20.50")), Stock("BAD", D("20.50")), Stock("THIRD", D("20.50")),
        Stock("DEAD", D("20.50"), status="inactive"),  # delisted since: survivorship
        Stock("REVS", D("0.80"), daily_range=D("0.06")),  # a 1:10 reverse split comes later
        Stock("SPY", D("500.00"), daily_range=D("5.0"), adv=50_000_000, exchange="ARCA"),
    ]  # fmt: skip


async def test_the_backtest_runs_the_live_rules_on_history(cache: Journal) -> None:
    h = History(stocks())
    h.split_list.append(Split(symbol="REVS", ex_date=date(2026, 10, 20), process_date=None,
                              old_rate=D(10), new_rate=D(1), reverse=True))  # fmt: skip
    tue, wed = date(2026, 10, 6), date(2026, 10, 7)
    h.hot("GOOD", tue, minute=0, wins=True)
    h.hot("BAD", tue, minute=1, wins=False)
    h.hot("THIRD", tue, minute=2, wins=True)  # two positions are open at 09:42
    h.hot("DEAD", wed, minute=0, wins=True)
    h.hot("REVS", wed, minute=0, wins=True)  # $0.80 as of then; ATR 0.06
    result = await backtest(h, cache, FIRST_DAY, date(2026, 10, 9))
    assert len(result.days) == 5
    assert day_of(result, tue) == {
        "GOOD": (True, None), "BAD": (True, None), "THIRD": (False, "capacity_positions"),
    }  # fmt: skip
    assert day_of(result, wed) == {"DEAD": (True, None)}  # REVS never qualifies
    good = next(t for d in result.days for t in d.trades if t.symbol == "GOOD")
    # trigger 20.91, stop 20.81, entry 20.91 + 0.01 + 0.02% = 20.924182; notional cap binds:
    # floor($500 / limit 20.94) = 23 shares; out at the 15:55 open 21.50
    assert (good.qty, good.entry_price, good.exit_price) == (23, D("20.924182"), D("21.50"))
    bad = next(t for d in result.days for t in d.trades if t.symbol == "BAD")
    assert (bad.exit_reason, bad.exit_price) == ("stop", D("20.80"))
    net = sum((t.net_pnl for d in result.days for t in d.trades), D(0))
    assert result.days[-1].end_cash == D(1000) + net
    assert result.skips == {"capacity_positions": 1}
    assert result.spy_return == 0.0


class Picky(History):
    """Alpaca's answer to a symbol it won't serve: the whole request fails with a 400."""

    refused = "BOGUS"
    refusals = 0

    async def bars(self, symbols: Sequence[str], timeframe: str, start: datetime,
                   end: datetime | None = None, *, feed: str, adjustment: str = "raw",
                   asof: str | None = None) -> dict[str, list[Bar]]:  # fmt: skip
        if timeframe == "1Day" and self.refused in symbols:
            self.refusals += 1
            raise AlpacaError(400, f"invalid symbol: {self.refused}", "/v2/stocks/bars")
        return await super().bars(symbols, timeframe, start, end, feed=feed,
                                  adjustment=adjustment, asof=asof)  # fmt: skip


async def test_placeholders_and_refused_symbols_never_stop_the_fetch(cache: Journal) -> None:
    h = Picky([*stocks(), Stock("BOGUS", D("20.50")), Stock("0029900E0", D("20.50"))])
    tue = date(2026, 10, 6)
    h.hot("GOOD", tue, minute=0, wins=True)
    result = await backtest(h, cache, FIRST_DAY, date(2026, 10, 9))
    assert day_of(result, tue) == {"GOOD": (True, None)}
    assert (h.refusals, h.requests.count("1Day")) == (1, 1)  # refused with BOGUS, then served


async def test_settled_cash_waits_out_the_columbus_day_gap(cache: Journal) -> None:
    h = History([*stocks(), Stock("PRCY", D("50.50")), Stock("PRCZ", D("50.50"))])
    fri, mon, tue = date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13)
    h.hot("GOOD", fri, minute=0, wins=True)
    h.hot("THIRD", fri, minute=1, wins=True)
    h.hot("PRCY", mon, minute=0, wins=True)
    h.hot("PRCZ", tue, minute=0, wins=True)
    result = await backtest(h, cache, fri, tue)
    assert day_of(result, fri) == {"GOOD": (True, None), "THIRD": (True, None)}
    # Friday's sales settle Tuesday (Monday is a bank holiday): about $37 is left on Monday.
    assert day_of(result, mon) == {"PRCY": (False, "insufficient_settled_cash")}
    assert day_of(result, tue) == {"PRCZ": (True, None)}


async def test_the_as_of_view_never_looks_ahead(cache: Journal) -> None:
    h = History([Stock("SPLT", D("40.00")), *stocks()])
    split_day = date(2026, 10, 7)
    for i, b in enumerate(h.daily["SPLT"]):  # trades at half the price from the split on
        if b.ts.date() >= split_day:
            h.daily["SPLT"][i] = bar(b.ts, D(20), D("20.5"), D("19.5"), D(20), 4_000_000)
    h.split_list += [
        Split(symbol="SPLT", ex_date=split_day, process_date=None, old_rate=D(1), new_rate=D(2),
              reverse=False),
        Split(symbol="REVS", ex_date=date(2026, 10, 20), process_date=None, old_rate=D(10),
              new_rate=D(1), reverse=True),
    ]  # fmt: skip
    data = BacktestData(source=h, cache=cache, calendar=CALENDAR)
    await data.prepare(FIRST_DAY, date(2026, 10, 9), symbol_ok=lambda _a: True)
    start = at(date(2026, 10, 1), 0, 0)
    before = await data.at(date(2026, 10, 6)).bars(["SPLT", "REVS"], "1Day", start, feed="sip",
                                                    adjustment="split")  # fmt: skip
    assert {b.close for b in before["SPLT"]} == {D(40)}  # the split is still in the future
    assert max(b.ts.date() for b in before["SPLT"]) == date(2026, 10, 5)  # nothing from day t
    after = await data.at(date(2026, 10, 8)).bars(["SPLT", "REVS"], "1Day", start, feed="sip",
                                                   adjustment="split")  # fmt: skip
    assert {b.close for b in after["SPLT"]} == {D(20)}  # restated in the new share basis
    assert after["SPLT"][0].volume == 4_000_000  # 2,000,000 pre-split shares x 2
    assert {b.close for b in after["REVS"]} == {D("0.80")}  # 1:10 on 10-20 isn't known yet
    raw = await data.at(date(2026, 10, 8)).bars(["SPLT"], "1Day", start, feed="sip")
    assert {b.close for b in raw["SPLT"]} == {D(40), D(20)}
    view = data.at(date(2026, 10, 8))
    assert [s.symbol for s in await view.splits(date(2026, 9, 1), date(2026, 12, 31))] == ["SPLT"]
    with pytest.raises(LookAheadError):
        await view.bars(["SPLT"], "5Min", at(date(2026, 10, 9), 9, 30), feed="iex")


async def test_a_cached_rerun_makes_no_requests(cache: Journal) -> None:
    h = History(stocks())
    h.hot("GOOD", date(2026, 10, 6), minute=0, wins=True)
    first = await backtest(h, cache, FIRST_DAY, date(2026, 10, 9))
    assert first.requests > 0
    made = len(h.requests)
    again = await backtest(h, cache, FIRST_DAY, date(2026, 10, 9))
    assert again.requests == 0
    assert len(h.requests) == made
    assert [len(d.trades) for d in again.days] == [len(d.trades) for d in first.days]


async def test_the_report(cache: Journal, tmp_path: Path) -> None:
    h = History(stocks())
    h.hot("GOOD", date(2026, 10, 6), minute=0, wins=True)
    h.hot("BAD", date(2026, 10, 7), minute=0, wins=False)
    result = await backtest(h, cache, FIRST_DAY, date(2026, 10, 9))
    md, table = report.write(result, tmp_path / "reports", tmp_path / "data")
    text = md.read_text(encoding="utf-8")
    assert "## Book A long-only leg" in text
    assert "- trades: 2; win rate: 50.0%" in text
    assert "## H4: mean shadow R by rel_vol rank" in text
    assert "insufficient data" in text  # only ranks 1-5 have signals here
    rows = pq.read_table(table).to_pylist()
    assert [(r["symbol"], r["traded"], r["exit_reason"]) for r in rows] == [
        ("GOOD", True, "flatten"), ("BAD", True, "stop"),
    ]  # fmt: skip


def test_h4_verdicts() -> None:
    s = report.ShadowStats
    falling = [("1-5", s(9, 0.5, 0.4)), ("6-10", s(9, 0.5, 0.2)), ("11-15", s(9, 0.5, 0.1)),
               ("16-20", s(9, 0.5, -0.1))]  # fmt: skip
    assert report.h4_verdict(falling).startswith("consistent with H4: mean shadow R falls")
    bumpy = [falling[0], falling[2], falling[1], falling[3]]
    assert "not monotone" in report.h4_verdict(bumpy)
    assert report.h4_verdict(list(reversed(falling))).startswith("not consistent")


def test_busy_hours() -> None:
    monday = date(2026, 10, 12)
    assert busy_hours(CALENDAR, at(monday, 9, 0))
    assert not busy_hours(CALENDAR, at(monday, 8, 59))
    assert not busy_hours(CALENDAR, at(monday, 16, 30))
    assert not busy_hours(CALENDAR, at(date(2026, 10, 10), 12, 0))  # a Saturday


class Rest(History):
    async def calendar(self, start: date, end: date) -> list[dict[str, str]]:
        return nyse.calendar_rows(max(start, nyse.FIRST), min(end, nyse.LAST))

    async def __aenter__(self) -> "Rest":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def test_cli_refuses_market_hours_and_unfinished_days(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = JumpClock(at(date(2026, 10, 13), 10, 0))
    monkeypatch.setattr(runtime, "RealClock", lambda: clock)
    monkeypatch.setattr(runtime.AlpacaRest, "from_config", lambda _c, _k: Rest(stocks()))

    def run(*args: str) -> tuple[int, str]:
        result = CliRunner().invoke(app, ["--root", str(repo), "backtest", *args])
        return result.exit_code, result.output

    code, out = run("--start", "2026-10-05", "--end", "2026-10-09")
    assert code == 1
    assert "doesn't run 09:00-16:30" in out
    code, out = run("--start", "2026-10-05", "--end", "2026-10-13", "--force")
    assert code == 1
    assert "before today" in out
    code, out = run("--start", "2026-10-05", "--end", "2026-10-09", "--force")
    assert code == 0, out
    assert "5/5 2026-10-09" in out
    assert (repo / "runtime" / "reports" / "backtest-2026-10-05-2026-10-09-iex.md").exists()
