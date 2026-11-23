"""The weekly report (P5.4; A§4.10; DECISIONS D-078): what it gathers for a week and its epoch,
how it renders (the model's text inert in both formats), the SVG curves, the weeks, SPY's missing
closes, and the CLI."""

import sqlite3
from collections.abc import Iterator, Sequence
from datetime import date, datetime, timedelta
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
    journal,
    sessions,
)
from trader.cli import app
from trader.core.clock import ET, SimClock
from trader.core.models import Bar
from trader.memory.journal import Journal, open_database
from trader.memory.stats import Bootstrap
from trader.reports.weekly import (
    EquityPoint,
    build_weekly,
    equity_svg,
    fill_spy_closes,
    iso_week,
    last_complete_week,
    render_html,
    render_markdown,
    write_weekly,
)

BOOT = Bootstrap.of(SETTINGS.lessons)
W45 = sessions(date(2026, 11, 2), 5)  # Mon 11-02 to Fri 11-06
W46 = sessions(date(2026, 11, 9), 5)  # Mon 11-09 to Fri 11-13
TRICKY = "see `x` | [a](http://x.io) <script>alert(1)</script>"


def fill(
    conn: sqlite3.Connection, *, books: Sequence[str] = ("A", "B"), last_spy: bool = True
) -> None:
    """Two weeks: each day a winner both books take and a loser Book B vetoes; equity drifts."""
    add_spy(conn, date(2026, 10, 30), "100")
    equity = {"A": Decimal(1000), "B": Decimal(1000)}
    for k, day in enumerate(W45 + W46):
        add_session(conn, day)
        if last_spy or day != W46[-1]:
            add_spy(conn, day, str(100 + k))
        add_long(conn, day, "WIN", shadow_r=1.0, verdict="allow")
        add_long(conn, day, "LOSE", shadow_r=-1.0, verdict="veto", rank=2)
        for book in books:
            add_trade(conn, day, book, "WIN", r=1.0, pnl="2.00")
            if book == "A":
                add_trade(conn, day, book, "LOSE", r=-1.0, pnl="-1.00", entry="10:00:00",
                          exit_="10:30:00")  # fmt: skip
            start = equity[book]
            equity[book] = start + (Decimal("0.96") if book == "A" else Decimal("1.98"))
            add_book_day(conn, day, book, start=str(start), end=str(equity[book]))
    conn.execute(
        "INSERT INTO signal_reviews (signal_id, date, went_well, went_badly, why, factors_json, "
        "knowable_at_decision, b_process, b_note, eval_epoch) VALUES (?, ?, '', '', 'w', "
        "'[\"false_breakout\"]', 'yes', 'bad_process', ?, 0)",
        (f"{W46[1]:%Y%m%d}-LOSE-orb5", W46[1].isoformat(), TRICKY),
    )
    conn.executescript(
        "INSERT INTO lessons (id, statement, status, n_subset, effect_r) VALUES "
        "('L-001', 'Big gaps fade', 'promoted', 30, -0.3), ('L-002', 'p', 'proposed', NULL, NULL),"
        " ('L-003', 'r', 'rejected', 25, 0.01);"
        "INSERT INTO process_issues (first_seen, last_seen, occurrences, severity, component, "
        "description, status) VALUES ('2026-11-03', '2026-11-12', 4, 'warn', 'execution', "
        "'fills lag | at the open', 'open');"
    )
    conn.execute(
        "INSERT INTO incidents (ts, severity, component, code, message) VALUES "
        "('2026-11-04T09:40:00-05:00', 'P3', 'x', 'old_week', 'w45'), "
        "(?, 'P2', 'llm', 'llm_call_failed', ?)",
        (f"{W46[2].isoformat()}T09:41:00-05:00", TRICKY),
    )


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = journal(tmp_path / "trader.db")
    yield c
    c.close()


# ---------------------------------------------------------------- the weeks


def test_the_last_complete_week() -> None:
    assert last_complete_week(date(2026, 11, 21)) == (2026, 47)  # a Saturday: this week
    assert last_complete_week(date(2026, 11, 22)) == (2026, 47)  # Sunday too
    assert last_complete_week(date(2026, 11, 25)) == (2026, 47)  # Wednesday: last week
    assert last_complete_week(date(2027, 1, 4)) == (2026, 53)  # across the year
    assert iso_week(2026, 46) == (date(2026, 11, 9), date(2026, 11, 13))


# ---------------------------------------------------------------- what it gathers


def test_a_week_with_its_epoch_to_date(conn: sqlite3.Connection) -> None:
    fill(conn)
    r = build_weekly(conn, year=2026, week=46, boot=BOOT)
    assert (r.first, r.last, r.epoch) == (W46[0], W46[-1], 0)
    assert {b: m.days for b, m in r.week_stats.books.items()} == {"A": 5, "B": 5}
    assert {b: m.trades for b, m in r.epoch_stats.books.items()} == {"A": 20, "B": 10}
    assert len(r.curve) == 10
    first, last = r.curve[0], r.curve[-1]
    assert (first.a, first.b, first.spy) == (pytest.approx(100.096), pytest.approx(100.198),
                                            pytest.approx(100.0))  # fmt: skip
    assert last.spy == pytest.approx(109.0)  # 109 against the 10-30 close of 100
    assert r.spy_note is None
    veto = r.epoch_stats.veto
    assert (veto.n_allowed, veto.n_vetoed, veto.v, veto.mean_vetoed) == (10, 10, 2.0, -1.0)
    assert r.lessons == {"promoted": 1, "proposed": 1, "rejected": 1}
    assert r.promoted == (("L-001", "Big gaps fade", 30, -0.3),)
    assert [(f.factor, f.targets) for f in r.factors] == [("false_breakout", 1)]
    assert r.bad_process == ((W46[1].isoformat(), "LOSE", TRICKY),)
    assert [(f.kind, f.count) for f in r.followups] == [("issue", 4)]
    assert [code for _, _, _, code, _ in r.incidents] == ["llm_call_failed"]  # W45's left out
    ((epoch0, diff),) = r.epochs
    assert (epoch0.epoch, epoch0.decision, diff.value) == (0, "D-075", pytest.approx(1.0 - 0.0))


def test_a_missing_last_close_is_said(conn: sqlite3.Connection) -> None:
    fill(conn, last_spy=False)
    r = build_weekly(conn, year=2026, week=46, boot=BOOT)
    assert r.spy_note == f"SPY's closes reach {W46[-2]}, not the books' last session ({W46[-1]})"
    assert r.curve[-1].spy is None


def test_a_week_without_book_b_and_an_empty_week(conn: sqlite3.Connection) -> None:
    fill(conn, books=("A",))
    r = build_weekly(conn, year=2026, week=46, boot=BOOT)
    assert sorted(r.week_stats.books) == ["A"]
    assert all(p.b is None for p in r.curve)
    empty = build_weekly(conn, year=2026, week=50, boot=BOOT)
    assert empty.week_stats.books == {}
    assert "(none)" in render_markdown(empty)


# ---------------------------------------------------------------- rendering


def test_the_model_s_text_stays_inert_in_both_formats(conn: sqlite3.Connection) -> None:
    fill(conn)
    r = build_weekly(conn, year=2026, week=46, boot=BOOT)
    md = render_markdown(r)
    assert "# Weekly report 2026-W46 (2026-11-09 to 2026-11-13)" in md
    assert "``see `x` \\| [a](http://x.io) <script>alert(1)</script>``" in md  # a code span
    assert "fills lag \\| at the open" in md  # the model's issue text, its pipe escaped
    assert "## Limitations" in md
    page = render_html(r)
    assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert page.count("<polyline") == 3


def test_the_svg_curves() -> None:
    curve = [EquityPoint(date(2026, 11, 9) + timedelta(days=k), 100 + k, 100 - k, None)
             for k in range(3)]  # fmt: skip
    svg = equity_svg(curve)
    assert svg.count("<polyline") == 2  # no SPY points: no line for it
    assert "102.0" in svg
    assert "98.0" in svg
    assert equity_svg([]) == "<p>(no equity to chart)</p>"
    one = equity_svg([EquityPoint(date(2026, 11, 9), 100.0, 100.0, 100.0)])
    assert one.count("<polyline") == 3  # a single session draws, flat


def test_written_by_week(conn: sqlite3.Connection, tmp_path: Path) -> None:
    fill(conn)
    md, page = write_weekly(build_weekly(conn, year=2026, week=46, boot=BOOT), tmp_path / "r")
    assert (md.name, page.name) == ("weekly-2026-46.md", "weekly-2026-46.html")


# ---------------------------------------------------------------- SPY's missing closes


class Spy:
    """A year of SPY daily bars, or none (an outage)."""

    def __init__(self, *, down: bool = False) -> None:
        self.down, self.asked = down, 0

    async def bars(
        self,
        symbols: Sequence[str],
        timeframe: str,
        start: datetime,
        end: datetime | None = None,
        *,
        feed: str,
        adjustment: str = "raw",
    ) -> Any:
        self.asked += 1
        if self.down:
            from trader.data.alpaca_rest import AlpacaError

            raise AlpacaError(503, "down", "-")
        days, day = [], date(2025, 11, 3)
        while day <= date(2026, 11, 13):
            if day.weekday() < 5:
                days.append(day)
            day += timedelta(days=1)
        bars = [
            Bar(ts=datetime.combine(d, datetime.min.time(), ET), open=Decimal(500 + k % 7),
                high=Decimal(505 + k % 7), low=Decimal(495 + k % 7), close=Decimal(501 + k % 7),
                volume=1_000_000)
            for k, d in enumerate(days)
        ]  # fmt: skip
        return {"SPY": bars}


async def test_spy_s_missing_closes_are_filled_by_the_boot_job_s_own_function(
    tmp_path: Path,
) -> None:
    j = await Journal.open(tmp_path / "trader.db", inline=True)
    clock = SimClock(datetime(2026, 11, 14, 10, tzinfo=ET))
    await j.write(lambda c: add_spy(c, W46[0], "500"))
    market = Spy()
    missing = await fill_spy_closes(j, market, clock, W46)
    assert missing == []
    assert market.asked == 4  # Monday's was there
    rows = await j.fetch_all("SELECT date FROM regime_daily ORDER BY date")
    assert [r["date"] for r in rows] == [d.isoformat() for d in W46]
    assert await fill_spy_closes(j, Spy(down=True), clock, [date(2026, 11, 16)]) == [
        date(2026, 11, 16)
    ]
    await j.aclose()


# ---------------------------------------------------------------- the CLI


def test_trader_report_weekly(repo: Path) -> None:
    db = open_database(repo / "runtime" / "trader.db")
    db.row_factory = sqlite3.Row
    fill(db)
    db.commit()
    db.close()

    def run(*args: str) -> Any:
        return CliRunner().invoke(app, ["--root", str(repo), "report", "weekly", *args])

    done = run("--week", "2026-W46", "--offline")
    assert done.exit_code == 0, done.output
    said = done.output.splitlines()[0]
    assert said == "week 2026-W46: 10 sessions in epoch 0; V +2.000 (n 10/10)"
    assert (repo / "runtime" / "reports" / "weekly-2026-46.md").is_file()
    bad = run("--week", "46", "--offline")
    assert bad.exit_code == 1
    assert "--week takes YYYY-Www" in bad.output
