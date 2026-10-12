"""The 09:35 opening-range scan (S§3.1-3.2, P3.1; DECISIONS D-040)."""

import sqlite3
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fixtures import nyse
from tests.fixtures.exec_harness import CALENDAR, RISK, SETTINGS
from tests.fixtures.simtime import JumpClock
from trader import runtime
from trader.cli import app
from trader.config import Settings, StrategyConfig
from trader.core.clock import ET
from trader.core.models import Bar, Signal, Split
from trader.data.alpaca_rest import AlpacaError, Feed
from trader.data.recorder import OR_SCAN_FILE, Recorder, read_table
from trader.memory.journal import Journal, open_database
from trader.strategy.orb_sip import (
    Candidate,
    OrbSipSelector,
    ScanError,
    ScanRow,
    last_scanned_session,
    load_signals,
    select,
    selected,
    suspect_gap,
    to_signal,
)
from trader.strategy.pricing import entry_limit, is_chased, long_prices, short_prices

D = Decimal
DAY = date(2026, 10, 13)  # a Tuesday; the previous session is Columbus Day (NYSE open)
PREV = date(2026, 10, 12)
OPEN = datetime.combine(DAY, time(9, 30), ET)
SCAN_AT = datetime.combine(DAY, time(9, 35, 3), ET)
DEADLINE = datetime.combine(DAY, time(9, 36, 30), ET)
STRATEGY = SETTINGS.strategy
CHASE = RISK.max_chase_pct


def cand(symbol: str, *, prev: str = "12.00", atr: str = "0.80", avg: float = 5_000.0) -> Candidate:
    return Candidate(symbol=symbol, prev_close=D(prev), atr14=D(atr), adv14=2_000_000,
                     or_vol_avg14=avg)  # fmt: skip


def bar(o: str = "12.40", h: str = "12.71", low: str = "12.30", c: str = "12.65",
        v: int = 50_000, ts: datetime = OPEN) -> Bar:  # fmt: skip
    return Bar(ts=ts, open=D(o), high=D(h), low=D(low), close=D(c), volume=v)


def outcomes(rows: Sequence[ScanRow]) -> dict[str, str]:
    return {row.symbol: row.outcome for row in rows}


def run(cands: Sequence[Candidate], bars: dict[str, Bar], *, feed: Feed = "iex",
        splits: frozenset[str] = frozenset(),
        strategy: StrategyConfig = STRATEGY) -> list[ScanRow]:  # fmt: skip
    return select(DAY, cands, bars, strategy=strategy, feed=feed, split_symbols=splits)


# ---------------------------------------------------------------- the rules


def test_each_filter_at_its_exact_boundary() -> None:
    cands = [
        cand("VOLA", avg=500.0), cand("VOLB", avg=500.0),  # OR volume 999 vs 1000
        cand("AVGA", avg=199.99), cand("AVGB", avg=200.0),  # baseline 199.99 vs 200
        cand("PRCA", prev="5.00"), cand("PRCB", prev="5.01"),  # open 5.00 vs 5.01
        cand("RVLA", avg=10_000.0), cand("RVLB", avg=10_000.0),  # rel_vol 0.9999 vs 1.0
    ]  # fmt: skip
    bars = {
        "VOLA": bar(v=999), "VOLB": bar(v=1_000),
        "AVGA": bar(v=5_000), "AVGB": bar(v=5_000),
        "PRCA": bar(o="5.00", h="5.10", low="4.98", c="5.05"),
        "PRCB": bar(o="5.01", h="5.10", low="4.98", c="5.05"),
        "RVLA": bar(v=9_999), "RVLB": bar(v=10_000),
    }  # fmt: skip
    assert outcomes(run(cands, bars)) == {
        "VOLA": "or_volume", "VOLB": "selected",
        "AVGA": "or_vol_avg", "AVGB": "selected",
        "PRCA": "price", "PRCB": "selected",
        "RVLA": "rel_vol", "RVLB": "selected",
    }  # fmt: skip


def test_the_sip_feed_drops_the_iex_liquidity_guards() -> None:
    cands = [cand("THIN", avg=50.0)]
    bars = {"THIN": bar(v=60)}  # far under both IEX guards; rel_vol 1.2
    assert outcomes(run(cands, bars, feed="iex")) == {"THIN": "or_volume"}
    assert outcomes(run(cands, bars, feed="sip")) == {"THIN": "selected"}


def test_missing_bar_and_zero_baseline() -> None:
    rows = run([cand("NONE"), cand("ZERO", avg=0.0)], {"ZERO": bar()}, feed="sip")
    assert outcomes(rows) == {"NONE": "no_bar", "ZERO": "rel_vol"}
    none = next(r for r in rows if r.symbol == "NONE")
    assert (none.or_volume, none.or_open, none.rel_vol) == (0, None, None)


def test_ranking_breaks_ties_on_volume_then_symbol_and_cuts_at_top_n() -> None:
    # 22 survivors: all rel_vol 2.0 except two; ties on rel_vol go to the bigger OR volume,
    # then to the symbol.
    cands = [cand(f"S{i:02d}", avg=5_000.0) for i in range(20)]
    cands += [cand("TOPA", avg=1_000.0), cand("TOPB", avg=2_000.0)]
    bars = {f"S{i:02d}": bar(v=10_000) for i in range(20)}
    bars |= {"TOPA": bar(v=5_000), "TOPB": bar(v=10_000)}  # both rel_vol 5.0
    bars["S07"] = bar(v=10_001)  # a hair more volume: same rank band, ahead of the other S..
    rows = run(cands, bars)
    top = selected(rows)
    assert [r.symbol for r in top[:4]] == ["TOPB", "TOPA", "S07", "S00"]
    assert [r.rank for r in top] == list(range(1, 21))
    cut = sorted((r for r in rows if r.outcome == "rank"), key=lambda r: r.rank or 0)
    assert [(r.symbol, r.rank) for r in cut] == [("S18", 21), ("S19", 22)]


def test_splits_and_suspect_gaps_never_take_a_slot() -> None:
    cands = [cand("SPLT"), cand("HALF", prev="24.80"), cand("NEAR", prev="23.80"),
             cand("REVX", prev="1.24"), cand("OKAY")]  # fmt: skip
    hot = bar(v=500_000)  # rel_vol 100: would rank first
    bars = {"SPLT": hot, "HALF": hot, "NEAR": hot, "REVX": hot, "OKAY": bar()}
    rows = run(cands, bars, splits=frozenset({"SPLT"}))
    assert outcomes(rows) == {
        "SPLT": "split",
        "HALF": "suspect_gap",  # 12.40 / 24.80 = 0.500: a 2:1 split Alpaca hasn't listed
        "NEAR": "selected",  # 12.40 / 23.80 = 0.521: 4.2% from 1/2, a real gap
        "REVX": "suspect_gap",  # 12.40 / 1.24 = 10: a 1:10 reverse split
        "OKAY": "selected",
    }
    assert selected(rows)[0].symbol == "NEAR"


def test_suspect_gap_tolerance() -> None:
    assert suspect_gap(D("10.30"), D("20.00"))  # 0.515: within 3% of 0.5
    assert not suspect_gap(D("10.31"), D("20.00"))
    assert suspect_gap(D("97.00"), D("10.00"))  # 9.7: within 3% of 10
    assert not suspect_gap(D("12.40"), D("12.00"))
    assert not suspect_gap(D("12.40"), D("0"))


# ---------------------------------------------------------------- signals and prices


def first_signal(row_bar: Bar, **kw: Any) -> Signal:
    row = selected(run([cand("XYZ")], {"XYZ": row_bar}))[0]
    return to_signal(row, strategy=STRATEGY, max_chase_pct=CHASE, **kw)


def test_a_long_signal_carries_valid_tick_prices() -> None:
    sig = first_signal(bar())
    assert (sig.direction, sig.status, sig.id) == ("long", "armed", "20261013-XYZ-orb5")
    # trigger 12.71 + 0.01; stop floor(12.72 - 0.10 x 0.80); chase floor(12.72 x 1.003 = 12.758);
    # limit min(max(ceil(12.72 x 1.001 = 12.7327), 12.72 + 0.02), 12.75)
    prices = (sig.trigger_price, sig.stop_price, sig.max_chase_price, sig.entry_limit)
    assert prices == (D("12.72"), D("12.64"), D("12.75"), D("12.74"))


def test_short_and_doji_rows() -> None:
    short = first_signal(bar(c="12.35"))
    assert (short.direction, short.status) == ("shadow_short", "not_armed")
    # trigger 12.30 - 0.01; stop ceil(12.29 + 0.08); no chase or limit: never traded
    assert (short.trigger_price, short.stop_price) == (D("12.29"), D("12.37"))
    assert (short.max_chase_price, short.entry_limit) == (None, None)
    doji = first_signal(bar(c="12.40"))
    assert (doji.direction, doji.status, doji.trigger_price) == ("none", "not_armed", None)


def test_shorts_get_no_prices_when_not_recorded() -> None:
    data = SETTINGS.model_dump()
    data["strategy"]["selection"]["record_shadow_shorts"] = False
    strategy = Settings.model_validate(data).strategy
    row = selected(run([cand("XYZ")], {"XYZ": bar(c="12.35")}, strategy=strategy))[0]
    short = to_signal(row, strategy=strategy, max_chase_pct=CHASE)
    assert (short.direction, short.trigger_price, short.stop_price) == ("shadow_short", None, None)


def test_features_are_pre_decision_values() -> None:
    sig = first_signal(bar(), features={"day_of_week": "Tue", "event_day": False})
    feats = sig.features
    assert feats["or_range_atr"] == pytest.approx(0.5125)  # (12.71 - 12.30) / 0.80
    assert feats["or_return_pct"] == pytest.approx(2.0161, abs=1e-4)
    assert feats["gap_pct"] == pytest.approx(3.3333, abs=1e-4)
    assert feats["atr_pct"] == pytest.approx(6.6667, abs=1e-4)
    assert (feats["day_of_week"], feats["event_day"]) == ("Tue", False)


def test_a_flagged_scan_arms_nothing() -> None:
    sig = first_signal(bar(), flag="scan_late")
    assert (sig.status, sig.features["scan_late"]) == ("not_armed", True)


def test_entry_limit_and_chase_guard() -> None:
    lp = long_prices(D("12.71"), D("0.80"), entry=STRATEGY.entry, exit=STRATEGY.exit,
                     max_chase_pct=CHASE)  # fmt: skip
    limit = lambda last: entry_limit(lp.trigger, D(last), entry=STRATEGY.entry,  # noqa: E731
                                     max_chase=lp.max_chase)  # fmt: skip
    assert limit("12.72") == D("12.74")  # the + $0.02 floor binds
    assert limit("12.74") == D("12.75")  # ceil(12.74 x 1.001 = 12.7527) capped at 12.75 anyway
    assert limit("12.80") == D("12.75")  # never above the chase cap
    assert not is_chased(D("12.75"), lp.max_chase)
    assert is_chased(D("12.76"), lp.max_chase)
    sp = short_prices(D("12.30"), D("0.80"), entry=STRATEGY.entry, exit=STRATEGY.exit)
    assert (sp.trigger, sp.stop) == (D("12.29"), D("12.37"))


# ---------------------------------------------------------------- the live scan


class Market:
    """Scan inputs; the ready symbol's 09:34 bar appears at ``ready_at``."""

    def __init__(self, clock: JumpClock, bars: dict[str, Bar], *, ready_at: datetime | None = None,
                 splits: Sequence[Split] = ()) -> None:  # fmt: skip
        self.clock, self.or_bars, self.ready_at, self.split_list = clock, bars, ready_at, splits
        self.requests: list[tuple[str, tuple[str, ...]]] = []
        self.fail_ready = 0

    async def bars(self, symbols: Sequence[str], timeframe: str, start: datetime,
                   end: datetime | None = None, *, feed: str, adjustment: str = "raw",
                   ) -> dict[str, list[Bar]]:  # fmt: skip
        self.requests.append((timeframe, tuple(symbols)))
        if timeframe == "1Min":
            if self.fail_ready:
                self.fail_ready -= 1
                raise AlpacaError(500, "flaky", "/v2/stocks/bars")
            ready = self.ready_at is None or self.clock.now() >= self.ready_at
            return {"SPY": [bar(ts=start)]} if ready else {}
        assert (timeframe, start, end, adjustment) == (
            "5Min", OPEN, OPEN + timedelta(minutes=5) - timedelta(seconds=1), "split",
        )  # fmt: skip
        return {s: [self.or_bars[s]] for s in symbols if s in self.or_bars}

    async def splits(self, start: date, end: date) -> list[Split]:
        return [s for s in self.split_list if start <= s.ex_date <= end]


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    yield opened
    await opened.aclose()


async def universe(journal: Journal, cands: Sequence[Candidate], day: date = DAY) -> None:
    await journal.write(lambda conn: conn.executemany(
        "INSERT INTO universe_daily (date, symbol, prev_close, atr14, adv14, or_vol_avg14, "
        "eligible) VALUES (?, ?, ?, ?, ?, ?, 1)",
        [(day.isoformat(), c.symbol, str(c.prev_close), str(c.atr14), c.adv14, c.or_vol_avg14)
         for c in cands],
    ))  # fmt: skip


def split(symbol: str, ex_date: date) -> Split:
    return Split(symbol=symbol, ex_date=ex_date, process_date=None, old_rate=D(1),
                 new_rate=D(2), reverse=False)  # fmt: skip


def selector(journal: Journal, market: Market, clock: JumpClock,
             recorder: Recorder | None = None) -> OrbSipSelector:  # fmt: skip
    return OrbSipSelector(market=market, journal=journal, clock=clock, calendar=CALENDAR,
                          strategy=STRATEGY, max_chase_pct=CHASE, feed="iex", ready_symbol="SPY",
                          recorder=recorder)  # fmt: skip


FIVE = [cand("AAA"), cand("BBB"), cand("CCC"), cand("DDD"), cand("SPL")]
FIVE_BARS = {
    "AAA": bar(v=40_000),  # long
    "BBB": bar(v=30_000, c="12.35"),  # shadow short
    "CCC": bar(v=20_000, c="12.40"),  # doji
    "DDD": bar(v=100),  # under the IEX guard
    "SPL": bar(v=90_000),  # a split on the previous session: dropped before ranking
}


async def test_scan_persists_the_top_rows_and_the_or_scan_table(
    journal: Journal, tmp_path: Path
) -> None:
    clock = JumpClock(SCAN_AT)
    await universe(journal, FIVE)
    recorder = Recorder(tmp_path / "recordings", DAY)
    market = Market(clock, FIVE_BARS, splits=[split("SPL", PREV), split("AAA", date(2026, 10, 20))])
    result = await selector(journal, market, clock, recorder).scan(DAY, deadline=DEADLINE)
    assert [(s.symbol, s.direction, s.status) for s in result.signals] == [
        ("AAA", "long", "armed"),
        ("BBB", "shadow_short", "not_armed"),
        ("CCC", "none", "not_armed"),
    ]  # fmt: skip
    assert result.counts == {"or_volume": 1, "selected": 3, "split": 1}
    assert (result.flag, result.bar_wait_s) == (None, 0.0)
    saved = await load_signals(journal, DAY)
    assert saved == list(result.signals)
    table = read_table(recorder.dir / OR_SCAN_FILE)
    assert {(r["symbol"], r["outcome"]) for r in table} == {
        ("AAA", "selected"), ("BBB", "selected"), ("CCC", "selected"), ("DDD", "or_volume"),
        ("SPL", "split"),
    }  # fmt: skip
    epoch = await journal.fetch_one("SELECT DISTINCT eval_epoch FROM signals")
    assert epoch is not None
    assert epoch[0] == 0


async def test_a_restart_returns_the_saved_signals_without_fetching(journal: Journal) -> None:
    clock = JumpClock(SCAN_AT)
    await universe(journal, FIVE)
    first = await selector(journal, Market(clock, FIVE_BARS), clock).scan(DAY)
    market = Market(clock, {})
    again = await selector(journal, market, clock).scan(DAY)
    assert again.resumed
    assert again.signals == first.signals
    assert market.requests == []


async def test_waits_for_the_last_opening_minute(journal: Journal) -> None:
    clock = JumpClock(SCAN_AT)
    await universe(journal, FIVE)
    ready = SCAN_AT + timedelta(seconds=1.5)
    market = Market(clock, FIVE_BARS, ready_at=ready)
    market.fail_ready = 1  # a failing check is retried, not fatal
    result = await selector(journal, market, clock).scan(DAY, deadline=DEADLINE)
    assert result.bar_wait_s == pytest.approx(1.5)
    assert [t for t, _ in market.requests].count("1Min") == 4  # 03.0 (fails), 03.5, 04.0, 04.5


async def test_gives_up_waiting_at_0935_08(journal: Journal) -> None:
    clock = JumpClock(SCAN_AT)
    await universe(journal, FIVE)
    market = Market(clock, FIVE_BARS, ready_at=SCAN_AT + timedelta(minutes=5))
    result = await selector(journal, market, clock).scan(DAY, deadline=DEADLINE)
    assert result.bar_wait_s == pytest.approx(5.0)  # 09:35:03 -> 09:35:08
    assert result.armed  # it still scans


async def test_a_past_session_needs_no_wait(journal: Journal) -> None:
    clock = JumpClock(datetime.combine(DAY, time(18, 0), ET))
    await universe(journal, FIVE)
    market = Market(clock, FIVE_BARS, ready_at=SCAN_AT + timedelta(hours=9))
    result = await selector(journal, market, clock).scan(DAY, persist=False)
    assert result.bar_wait_s == 0.0
    assert [t for t, _ in market.requests] == ["5Min"]


async def test_a_late_scan_still_saves_its_rows_not_armed(journal: Journal) -> None:
    clock = JumpClock(DEADLINE + timedelta(seconds=1))
    await universe(journal, FIVE)
    result = await selector(journal, Market(clock, FIVE_BARS), clock).scan(DAY, deadline=DEADLINE)
    assert (result.flag, result.armed) == ("scan_late", [])
    saved = await load_signals(journal, DAY)
    assert {s.status for s in saved} == {"not_armed"}
    assert all(s.features["scan_late"] is True for s in saved)


async def test_a_late_start_is_flagged_by_the_caller(journal: Journal) -> None:
    clock = JumpClock(datetime.combine(DAY, time(11, 0), ET))
    await universe(journal, FIVE)
    result = await selector(journal, Market(clock, FIVE_BARS), clock).scan(DAY, late="late_start")
    assert (result.flag, result.armed) == ("late_start", [])
    assert result.signals[0].features["late_start"] is True


async def test_no_universe_is_an_error(journal: Journal) -> None:
    clock = JumpClock(SCAN_AT)
    with pytest.raises(ScanError, match="trader universe build"):
        await selector(journal, Market(clock, {}), clock).scan(DAY)


async def test_a_dry_scan_writes_nothing(journal: Journal, tmp_path: Path) -> None:
    clock = JumpClock(SCAN_AT)
    await universe(journal, FIVE)
    recorder = Recorder(tmp_path / "recordings", DAY)
    sel = selector(journal, Market(clock, FIVE_BARS), clock, recorder)
    result = await sel.scan(DAY, persist=False)
    assert len(result.signals) == 4  # no splits listed here, so SPL counts
    assert await load_signals(journal, DAY) == []
    assert not (recorder.dir / OR_SCAN_FILE).exists()
    # ...and a dry scan recomputes even when a real one is already saved
    await sel.scan(DAY)
    assert (await sel.scan(DAY, persist=False)).resumed is False


# ---------------------------------------------------------------- `trader scan`


def test_last_scanned_session() -> None:
    at = SETTINGS.strategy.selection.scan_at
    assert last_scanned_session(CALENDAR, SCAN_AT, at) == DAY
    assert last_scanned_session(CALENDAR, SCAN_AT - timedelta(seconds=1), at) == PREV
    saturday = datetime(2026, 10, 17, 12, 0, tzinfo=ET)
    assert last_scanned_session(CALENDAR, saturday, at) == date(2026, 10, 16)


class Rest(Market):
    async def calendar(self, start: date, end: date) -> list[dict[str, str]]:
        return nyse.calendar_rows(max(start, nyse.FIRST), min(end, nyse.LAST))

    async def __aenter__(self) -> "Rest":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def test_cli_scan_prints_the_top_rows_and_saves_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = JumpClock(datetime.combine(DAY, time(18, 0), ET))
    db_path = repo / "runtime" / "trader.db"
    db = open_database(db_path)
    db.executemany(
        "INSERT INTO universe_daily (date, symbol, prev_close, atr14, adv14, or_vol_avg14, "
        "eligible) VALUES (?, ?, ?, ?, ?, ?, 1)",
        [(DAY.isoformat(), c.symbol, str(c.prev_close), str(c.atr14), c.adv14, c.or_vol_avg14)
         for c in FIVE],
    )  # fmt: skip
    db.close()
    monkeypatch.setattr(runtime, "RealClock", lambda: clock)
    monkeypatch.setattr(runtime.AlpacaRest, "from_config", lambda _c, _k: Rest(clock, FIVE_BARS))
    result = CliRunner().invoke(app, ["--root", str(repo), "scan", "--date", DAY.isoformat()])
    assert result.exit_code == 0, result.output
    assert "scan 2026-10-13 (iex): 5 eligible, 4 selected, 2 armed" in result.output
    assert "excluded: or_volume 1" in result.output
    assert " SPL " in result.output
    assert "shadow_short" in result.output
    check = sqlite3.connect(db_path)
    assert check.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
    check.close()
    missing = CliRunner().invoke(app, ["--root", str(repo), "scan", "--date", "2026-10-14"])
    assert missing.exit_code == 1
    assert "trader universe build" in missing.output
