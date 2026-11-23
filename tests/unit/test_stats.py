"""Measurement (P5.1; S§7.3; DECISIONS D-075): the day bootstrap, each book's metrics, the veto
value and its population, Book B minus Book A, SPY held, and the epoch record."""

import math
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.fixtures.exec_harness import SETTINGS
from tests.fixtures.journal_days import (
    add_book_day,
    add_long,
    add_session,
    add_spy,
    add_trade,
    at,
    decide,
    journal,
)
from trader.cli import app
from trader.core.clock import ET
from trader.memory.epochs import EpochError, current_epoch, epochs, start_epoch
from trader.memory.journal import open_database
from trader.memory.reviews import epoch_stats
from trader.memory.stats import (
    Bootstrap,
    DayRec,
    LongRec,
    TradeRec,
    b_minus_a,
    book_metrics,
    day_bootstrap,
    difference_interval,
    max_drawdown_pct,
    mean_interval,
    read_epoch,
    spy_buy_and_hold,
    spy_path,
    summarize,
    veto_value,
)
from trader.orchestrator.lock import LOCK_NAME, RunLock

D = Decimal
MON, TUE, WED, THU = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)
BOOT = Bootstrap.of(SETTINGS.lessons)  # 10,000 resamples, the settings' seed, 90%


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = journal(tmp_path / "trader.db")
    yield c
    c.close()


# ---------------------------------------------------------------- the bootstrap


def test_the_bootstrap_is_seeded_and_counts_what_it_skips() -> None:
    """A resample that draws only one side's days can't take the difference: it is skipped
    and counted. Two days, one per side: half the resamples draw one day twice."""
    by_day = [([1.0], []), ([], [-1.0])]
    first = difference_interval(by_day, BOOT)
    assert first is not None
    assert first == difference_interval(by_day, BOOT)  # the same seed, the same interval
    assert first.used + first.skipped == BOOT.resamples
    assert 3_000 < first.skipped < 7_000
    assert (first.low, first.high) == (2.0, 2.0)  # with both days drawn: 1 - (-1)


def test_resampling_whole_days_widens_the_interval_when_a_day_moves_together() -> None:
    """The human's choice (2026-11-21): one day's signals share the market. Ten days of five
    signals each, every signal of a day alike: drawn as days the mean varies as ten values do,
    drawn as signals as fifty would."""
    by_day = [[1.0 if k % 2 else -1.0] * 5 for k in range(10)]
    grouped = mean_interval(by_day, BOOT)
    single = mean_interval([[r] for day in by_day for r in day], BOOT)
    assert grouped is not None
    assert single is not None
    assert grouped.high - grouped.low > 1.5 * (single.high - single.low)


def test_no_days_no_interval() -> None:
    assert mean_interval([], BOOT) is None
    assert day_bootstrap(__import__("numpy").zeros((0, 2)), lambda t: t[:, 0], BOOT) is None


# ---------------------------------------------------------------- a book's metrics


def trade(book: str, day: date, r: float | None, pnl: str | None, *, fees: str = "0.02",
          entry: str = "09:41:05", exit_: str | None = "10:41:05",
          shadow: float | None = None) -> TradeRec:  # fmt: skip
    return TradeRec(
        book=book, day=day, signal_id=f"{day:%Y%m%d}-X{entry[:2]}-orb5", r=r,
        pnl=None if pnl is None else D(pnl), fees=D(fees), entry_slippage=D("0.02"),
        entry_delay_ms=1500, entry_ts=at(day, entry),
        exit_ts=at(day, exit_) if exit_ else None, shadow_r=shadow,
    )  # fmt: skip


def test_a_book_s_metrics_worked_by_hand(conn: sqlite3.Connection) -> None:
    """Three trades over two days; numbers checked by hand in the comments."""
    for day in (MON, TUE):
        add_session(conn, day)
    trades = [
        trade("A", MON, 2.0, "4.00", shadow=2.5),  # 09:41-10:41
        trade("A", MON, -1.0, "-2.00", entry="10:11:05", exit_="11:11:05", shadow=-1.0),
        trade("A", TUE, -1.0, "-1.00", fees="0.04", entry="09:50:00", exit_=None, shadow=-0.5),
        trade("B", TUE, 1.0, "1.00"),  # another book's: not counted
    ]
    add_book_day(conn, MON, "A", start="1000", end="1001.96")  # 4 - 2 - 0.04 of fees
    add_book_day(conn, TUE, "A", start="1001.96", end="1000.92")  # -1 - 0.04
    data = read_epoch(conn, 0, last=TUE)
    m = book_metrics("A", trades, data.days, data.sessions)
    assert (m.days, m.trades, m.win_rate, m.mean_r) == (2, 3, 1 / 3, 0.0)
    assert m.expectancy_gross_usd == pytest.approx(1 / 3)
    assert m.expectancy_net_usd == pytest.approx((3.98 - 2.02 - 1.04) / 3)
    assert m.profit_factor == pytest.approx(3.98 / (2.02 + 1.04))
    assert m.net_pnl_usd == pytest.approx(0.92)
    assert m.max_drawdown_pct == pytest.approx(1.04 / 1001.96 * 100)
    returns = [1.96 / 1000, -1.04 / 1001.96]
    mean, sd = sum(returns) / 2, abs(returns[0] - returns[1]) / math.sqrt(2)
    assert m.daily_sharpe == pytest.approx(mean / sd * math.sqrt(252))
    # Monday: 09:41-11:11 once (the trades overlap); Tuesday: 09:50 to the close, no exit
    assert m.exposure == pytest.approx((90 + 370) / (390 + 390))
    assert (m.mean_entry_slippage, m.mean_entry_delay_ms) == (0.02, 1500.0)
    # live R against the shadow's: -0.5, 0 and -0.5
    assert (m.live_minus_shadow_r, m.live_vs_shadow_n) == (pytest.approx(-1 / 3), 3)


def test_the_drawdown_counts_from_the_first_day_s_start() -> None:
    """A first day that loses is a drawdown from its own start, before any peak of its end."""
    days = [
        DayRec(MON, "A", D(1000), D(990), D(-10)),
        DayRec(TUE, "A", D(990), D(995), D(5)),
    ]
    assert max_drawdown_pct(days) == pytest.approx(1.0)
    assert max_drawdown_pct([]) is None


def test_an_empty_book_has_no_numbers() -> None:
    m = book_metrics("B", [], [], {})
    assert (m.trades, m.mean_r, m.profit_factor, m.max_drawdown_pct, m.daily_sharpe) == (
        0, None, None, None, None,
    )  # fmt: skip
    assert m.exposure is None


# ---------------------------------------------------------------- the veto value


def test_the_veto_value_s_population(conn: sqlite3.Connection) -> None:
    """D-075: triggered longs with a standing verdict; neutral counts as allow; a SIP-only
    trigger counts; a veto that never triggered, another epoch's signal and a smoke run's
    verdict don't; a triggered long with no verdict is counted apart."""
    add_long(conn, MON, "ALW", shadow_r=1.0, verdict="allow")
    add_long(conn, MON, "NEU", shadow_r=-1.0, verdict="neutral")
    add_long(conn, MON, "VET", shadow_r=-1.0, verdict="veto")
    add_long(conn, MON, "SIP", shadow_r=3.0, verdict="veto", live_trigger=False)
    add_long(conn, MON, "IDLE", shadow_r=None, verdict="veto")  # never triggered
    add_long(conn, MON, "NOV", shadow_r=0.5)  # Book B gave no verdict
    add_long(conn, MON, "OLD", shadow_r=5.0, verdict="veto", epoch=1)
    add_long(conn, TUE, "ALW", shadow_r=2.0, verdict="allow")
    conn.execute(  # a smoke run's veto on NEU, later than the vet's: never Book B's
        "INSERT INTO llm_calls (id, date, role, logical_id, attempt, final, ok, purpose) "
        "VALUES ('20261102-smoke-01-1', '2026-11-02', 'vet', '20261102-smoke-01', 1, 1, 1, "
        "'smoke')"
    )
    conn.execute(
        "INSERT INTO verdicts (date, symbol, source, verdict, confidence, llm_call_id) "
        "VALUES ('2026-11-02', 'NEU', 'vet', 'veto', 0.9, '20261102-smoke-01-1')"
    )
    v = veto_value(read_epoch(conn, 0, last=TUE).longs, BOOT)
    assert (v.n_allowed, v.n_vetoed, v.n_no_verdict) == (3, 2, 1)
    assert v.mean_allowed == pytest.approx(2 / 3)  # 1, -1 and 2
    assert v.mean_vetoed == pytest.approx(1.0)  # -1 and the SIP-only 3
    assert v.v == pytest.approx(-1 / 3)
    assert v.v_interval is not None
    assert v.vetoed_interval is not None


def test_a_veto_value_without_both_sides_has_no_interval() -> None:
    only = [LongRec(MON, "a", 1.0, "allow"), LongRec(TUE, "b", -1.0, "neutral")]
    v = veto_value(only, BOOT)
    assert (v.v, v.v_interval, v.vetoed_interval, v.n_vetoed) == (None, None, None, 0)


def test_the_review_s_stats_carry_the_veto_value(conn: sqlite3.Connection) -> None:
    add_long(conn, MON, "ALW", shadow_r=1.5, verdict="allow")
    add_long(conn, MON, "VET", shadow_r=-1.0, verdict="veto")
    stats = epoch_stats(conn, 0, MON, boot=BOOT)
    assert (stats["v_r"], stats["v_n_allowed"], stats["v_n_vetoed"]) == (2.5, 1, 1)
    assert (stats["v_ci_low"], stats["v_ci_high"], stats["v_vetoed_mean_r"]) == (2.5, 2.5, -1.0)
    assert "v_r" not in epoch_stats(conn, 0, MON)  # without the bootstrap's settings


# ---------------------------------------------------------------- the books compared, SPY


def test_book_b_minus_book_a() -> None:
    trades = [trade("B", MON, 1.0, "1"), trade("B", MON, 1.0, "1", entry="10:00:00"),
              trade("A", MON, -1.0, "-1"), trade("A", TUE, 0.0, "0")]  # fmt: skip
    d = b_minus_a(trades, BOOT)
    assert (d.value, d.n_first, d.n_second) == (1.5, 2, 2)
    assert d.interval is not None
    assert b_minus_a([trade("A", MON, 1.0, "1")], BOOT).value is None


def test_spy_held_over_the_books_sessions() -> None:
    spy = {MON: D(100), TUE: D(101), WED: D(99)}
    held = spy_buy_and_hold(spy, [TUE, WED])
    assert (held.start, held.end, held.covered) == (MON, WED, True)
    assert held.return_pct == pytest.approx(-1.0)
    late = spy_buy_and_hold(spy, [TUE, THU])  # Thursday's close isn't in yet
    assert (late.end, late.covered) == (WED, False)
    assert spy_buy_and_hold(spy, [MON]).return_pct is None  # no close before the first
    assert spy_buy_and_hold(spy, []).start is None
    assert spy_path(spy, [TUE, WED]) == {TUE: pytest.approx(101.0), WED: pytest.approx(99.0)}


def test_an_epoch_summary_from_the_journal(conn: sqlite3.Connection) -> None:
    for day, close in ((MON, "100"), (TUE, "102")):
        add_session(conn, day)
        add_spy(conn, day, close)
    add_spy(conn, date(2026, 10, 30), "99")
    add_long(conn, MON, "WIN", shadow_r=2.0, verdict="allow")
    add_long(conn, TUE, "LOSE", shadow_r=-1.0, verdict="veto")
    add_trade(conn, MON, "A", "WIN", r=2.0, pnl="4.00")
    add_trade(conn, MON, "B", "WIN", r=2.0, pnl="4.00", delay_ms=9000)
    add_trade(conn, TUE, "A", "LOSE", r=-1.0, pnl="-2.00")
    decide(conn, TUE, "LOSE", "B", "vetoed", "20261103-vet-01-1")
    for book, ends in (("A", ("1003.98", "1001.96")), ("B", ("1003.98", "1003.98"))):
        add_book_day(conn, MON, book, start="1000", end=ends[0])
        add_book_day(conn, TUE, book, start=ends[0], end=ends[1])
    s = summarize(read_epoch(conn, 0, last=TUE), BOOT)
    assert (s.first, s.last, sorted(s.books), s.triggered_longs) == (MON, TUE, ["A", "B"], 2)
    assert (s.books["A"].trades, s.books["B"].trades) == (2, 1)
    assert s.books["B"].mean_entry_delay_ms == 9000.0
    assert (s.veto.n_allowed, s.veto.n_vetoed, s.veto.v) == (1, 1, 3.0)
    assert s.b_minus_a.value == pytest.approx(2.0 - 0.5)
    assert (s.spy.start, s.spy.end) == (date(2026, 10, 30), TUE)
    assert s.spy.return_pct == pytest.approx(3 / 99 * 100)


# ---------------------------------------------------------------- the epoch record


def test_only_the_next_epoch_starts_and_it_is_recorded(conn: sqlite3.Connection) -> None:
    now = datetime(2026, 11, 21, 10, 0, tzinfo=ET)
    hashes = {"settings": "s" * 64, "risk": "r" * 64, "events": None}
    kw: dict[str, Any] = {"at": now, "reason": "the freeze", "note_id": None, "hashes": hashes}
    assert [(e.epoch, e.decision) for e in epochs(conn)] == [(0, "D-075")]
    with pytest.raises(EpochError, match="the next is 1, not 2"):
        start_epoch(conn, epoch=2, decision="D-090", **kw)
    with pytest.raises(EpochError, match="not a DECISIONS id"):
        start_epoch(conn, epoch=1, decision="90", **kw)
    with pytest.raises(EpochError, match="say why"):
        start_epoch(conn, epoch=1, decision="D-090", **(kw | {"reason": "  "}))
    with pytest.raises(EpochError, match="no review note 7"):
        start_epoch(conn, epoch=1, decision="D-090", **(kw | {"note_id": 7}))
    assert current_epoch(conn) == 0
    conn.execute("INSERT INTO meta (key, value) VALUES ('llm_fingerprint', 'fp')")
    row = start_epoch(conn, epoch=1, decision="D-090", **kw)
    assert current_epoch(conn) == 1
    assert epochs(conn)[-1] == row
    assert (row.settings_sha256, row.events_sha256, row.llm_fingerprint) == ("s" * 64, None, "fp")
    assert row.started_at == "2026-11-21T10:00:00-05:00"


def test_trader_epoch_shows_and_starts_one(repo: Path) -> None:
    db = open_database(repo / "runtime" / "trader.db")
    db.execute(
        "INSERT INTO review_notes (id, first_raised, last_raised, times_raised, note, status) "
        "VALUES (3, '2026-11-20', '2026-11-20', 1, 'tighter stops?', 'accepted')"
    )
    db.commit()
    db.close()

    def run(*args: str) -> Any:
        return CliRunner().invoke(app, ["--root", str(repo), "epoch", *args])

    shown = run("show")
    assert shown.exit_code == 0, shown.output
    assert shown.output.splitlines()[:2] == [
        "current epoch: 0",
        "  0  since the start  D-075: the build and its shakedown, before P7.1's freeze",
    ]
    assert run("start", "2", "--decision", "D-090", "--reason", "freeze").exit_code == 1
    with RunLock(repo / "runtime" / LOCK_NAME):  # a run-day is running
        busy = run("start", "1", "--decision", "D-090", "--reason", "freeze")
    assert busy.exit_code == 1
    assert "another `trader run-day` is running" in busy.output
    started = run("start", "1", "--decision", "D-090", "--reason", "the freeze", "--note", "3")
    assert started.exit_code == 0, started.output
    assert started.output.strip() == "epoch 1 begins (D-090): the freeze"
    with open_database(repo / "runtime" / "trader.db") as check:
        check.row_factory = sqlite3.Row
        row = epochs(check)[-1]
    assert (row.epoch, row.note_id, current_epoch(check)) == (1, 3, 1)
    assert row.settings_sha256 is not None
    assert len(row.settings_sha256) == 64
