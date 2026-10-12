"""The daily report (A§4.10, P3.7; DECISIONS D-046)."""

from collections.abc import AsyncIterator
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fixtures.exec_harness import CALENDAR
from trader.cli import app
from trader.core.clock import ET
from trader.core.models import Signal
from trader.memory.journal import Journal, open_database
from trader.reports.daily import build_daily, render_html, render_markdown, write_daily
from trader.strategy.orb_sip import save_signals

D = Decimal
DAY = date(2026, 10, 13)
NOW = datetime.combine(DAY, time(16, 30), ET)


def sig(symbol: str, rank: int, status: str = "triggered", direction: str = "long") -> Signal:
    return Signal(
        id=f"20261013-{symbol}-orb5", date=DAY, symbol=symbol, direction=direction,  # type: ignore[arg-type]
        status=status, rel_vol=9.5 - rank, rel_vol_rank=rank, or_open=D("12.40"),  # type: ignore[arg-type]
        or_high=D("12.71"), or_low=D("12.30"), or_close=D("12.65"), or_volume=50_000,
        atr14=D("0.80"), adv14=2_000_000, prev_close=D("12.00"), gap_pct=3.3,
        trigger_price=D("12.72"), stop_price=D("12.64"), max_chase_price=D("12.75"),
        triggered_at=datetime.combine(DAY, time(9, 41, 2), ET) if status == "triggered" else None,
        features={"regime_spy_trend": "up", "regime_vol_bucket": "mid", "event_day": False},
    )  # fmt: skip


async def fill_journal(journal: Journal) -> None:
    await save_signals(journal, [sig("XYZ", 1), sig("ABC", 2, "expired"),
                                 sig("SHRT", 3, "not_armed", "shadow_short")])  # fmt: skip
    statements = [
        ("INSERT INTO shadow_outcomes (signal_id, exit_reason, shadow_r) VALUES "
         "('20261013-XYZ-orb5', 'flatten', 2.5)", ()),
        ("INSERT INTO decisions (signal_id, book, take, reason_code, decided_at) VALUES "
         "('20261013-XYZ-orb5', 'A', 1, 'taken', ?)", (NOW.isoformat(),)),
        ("INSERT INTO trades (id, book, signal_id, symbol, qty, entry_ts, entry_price, stop_price, "
         "exit_ts, exit_price, exit_reason, pnl, r_multiple, fees_est) VALUES "
         "('A-20261013-XYZ-orb5', 'A', '20261013-XYZ-orb5', 'XYZ', 39, ?, '12.74', '12.64', ?, "
         "'13.00', 'eod_flatten', '10.14', 2.6, '0.02')",
         (datetime.combine(DAY, time(9, 41, 3), ET).isoformat(),
          datetime.combine(DAY, time(15, 55), ET).isoformat())),
        ("INSERT INTO daily_stats (date, book, start_equity, end_equity, pnl, regime_json) VALUES "
         "(?, 'A', '1000.00', '1010.12', '10.12', '{\"regime_spy_trend\": \"up\"}')",
         (DAY.isoformat(),)),
        ("INSERT INTO incidents (ts, severity, component, code, message) VALUES "
         "(?, 'P3', 'scan', 'scan_slow', 'took <script>alert(1)</script> 31 s')",
         (datetime.combine(DAY, time(9, 35, 40), ET).isoformat(),)),
        ("INSERT INTO halts (book, reason, scope, created_at) VALUES ('A', 'manual', "
         "'until_resume', ?)", (NOW.isoformat(),)),
    ]  # fmt: skip
    for sql, params in statements:
        await journal.execute(sql, params)


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    await fill_journal(opened)
    yield opened
    await opened.aclose()


async def test_the_report_reads_the_whole_day(journal: Journal, tmp_path: Path) -> None:
    report = await build_daily(journal, DAY, books=("A",), mode="paper",
                               session=CALENDAR.session(DAY), now=NOW)  # fmt: skip
    md = render_markdown(report)
    assert "# Daily report 2026-10-13" in md
    assert "session 09:30-16:00 ET | epoch 0 | mode paper | books A" in md
    assert "regime in force (prior session): regime_spy_trend up" in md
    assert "halts in force: A: manual" in md
    xyz = "| 1 | XYZ | 8.50 | long | triggered | 12.72 | 09:41:02 | 2.50 | flatten | "
    assert xyz + "taken R 2.60, +$10.12 | n/a |" in md
    assert "| 2 | ABC | 7.50 | long | expired | 12.72 |  |  |  |  | n/a |" in md
    assert "| A | 1000.00 | 1010.12 | +$10.12 | 1 | 1 |" in md
    assert "| XYZ | 39 | 12.74 | 13 | eod_flatten | +$10.14 | 0.02 | 2.60 |" in md
    assert "## Limitations" in md
    assert report.summary() == (
        "2026-10-13 | A: +$10.12 (1 trades, 1 won) | signals 3 (2 armed, 1 triggered) | "
        "incidents P1 0, P2 0"
    )
    page = render_html(report)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>" not in page
    md_path, html_path = write_daily(report, tmp_path / "reports")
    assert md_path.name == "2026-10-13.md"
    assert html_path.read_text(encoding="utf-8") == page


async def test_without_stats_the_pnl_comes_from_trades(journal: Journal) -> None:
    await journal.execute("DELETE FROM daily_stats")
    report = await build_daily(journal, DAY, books=("A", "B"), mode="paper", session=None, now=NOW)
    (a, b) = report.book_lines
    assert a.pnl == D("10.12")  # 10.14 gross - 0.02 fees
    assert (b.pnl, b.trades) == (D(0), ())
    assert "no session" in render_markdown(report)


def test_cli_regenerates_a_report_offline(repo: Path) -> None:
    db = open_database(repo / "runtime" / "trader.db")
    db.close()
    result = CliRunner().invoke(
        app, ["--root", str(repo), "report", "daily", "--date", "2026-10-13"]
    )
    assert result.exit_code == 0, result.output
    assert "2026-10-13 | A: +$0.00 (0 trades, 0 won)" in result.output
    assert (repo / "runtime" / "reports" / "2026-10-13.html").exists()
