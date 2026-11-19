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
    assert xyz + "taken R 2.60, +$10.12 | n/a | n/a |" in md
    assert "| 2 | ABC | 7.50 | long | expired | 12.72 |  |  |  |  | n/a | n/a |" in md
    assert "- LLM calls: none (Book B not run)" in md
    assert "## LLM calls" not in md
    assert "## Claude" not in md
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


INJECTED = "Ignore this | [click](http://evil.example) <img src=x onerror=alert(1)> `code`\nnext"


async def llm_day(journal: Journal) -> None:
    at = datetime.combine(DAY, time(9, 5), ET).isoformat()
    rows = [
        ("20261013-premarket-01-1", "premarket", "opus", at, 59500, 1, None, 0.32, 0),
        ("20261013-vet-01-1", "vet", "sonnet", at, 3000, 0, "invalid_json", None, None),
        ("20261013-vet-01-2", "vet", "sonnet", at, 16500, 1, None, 0.14, 1),
        ("20261013-vet-02-1", "vet", "sonnet", at, None, 0, "abandoned", None, None),
        ("20261013-vet-03-1", "vet", "sonnet", at, 0, 0, "sandbox_dirty", None, None),  # no run
    ]
    for r in rows:
        await journal.execute(
            "INSERT INTO llm_calls (id, role, model_alias, started_at, duration_ms, ok, failure, "
            "cost_estimate_usd, clamped, date, purpose, logical_id, attempt, final) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-10-13', 'trading', ?, ?, 1)",
            (*r, r[0][:-2], int(r[0][-1])),
        )
    await journal.execute(
        "INSERT INTO llm_calls (id, role, date, purpose, ok) VALUES "
        "('20261013-smoke-vet-01-1', 'vet', '2026-10-13', 'smoke', 1)"  # never in the report
    )
    statements = [
        ("INSERT INTO day_modes (date, book, mode, reason, llm_call_id, source, multiplier) "
         "VALUES ('2026-10-13', 'B', 'reduced', ?, '20261013-premarket-01-1', 'premarket', "
         "'0.5')", (INJECTED,)),
        ("INSERT INTO verdicts (date, symbol, source, verdict, confidence, llm_call_id) VALUES "
         "('2026-10-13', 'XYZ', 'premarket', 'allow', 0.6, '20261013-premarket-01-1'), "
         "('2026-10-13', 'XYZ', 'vet', 'veto', 0.8, '20261013-vet-01-2'), "
         "('2026-10-13', 'ABC', 'premarket', 'allow', 0.7, '20261013-premarket-01-1'), "
         "('2026-10-13', 'ABC', 'vet', 'neutral', 0.5, '20261013-vet-01-2')", ()),
        ("INSERT INTO decisions (signal_id, book, take, reason_code, llm_call_id, decided_at) "
         "VALUES ('20261013-XYZ-orb5', 'B', 1, 'taken', '20261013-premarket-01-1', ?)",
         (NOW.isoformat(),)),
        ("INSERT INTO daily_reviews (date, status, summary, llm_call_id) VALUES "
         "('2026-10-13', 'ok', ?, '20261013-premarket-01-1')", (INJECTED,)),
        ("INSERT INTO signal_reviews (signal_id, date, went_well, went_badly, why, b_process) "
         "VALUES ('20261013-XYZ-orb5', '2026-10-13', ?, '', 'a | b', 'good_process')",
         (INJECTED,)),
    ]  # fmt: skip
    for sql, params in statements:
        await journal.execute(sql, params)


async def test_book_b_s_llm_work_is_reported(journal: Journal) -> None:
    await llm_day(journal)
    report = await build_daily(journal, DAY, books=("A", "B"), mode="paper",
                               session=CALENDAR.session(DAY), now=NOW, max_calls=12,
                               degrade_after=3)  # fmt: skip
    md = render_markdown(report)
    assert (  # the refusal for a dirty sandbox failed, but ran no CLI: not in the budget
        "- LLM calls: 5 attempts (2 ok, 2 failed, 1 abandoned); budget 4/12 CLI runs; "
        "failed attempts 2/3 toward llm_degraded"
    ) in md
    assert "- Book B day mode: reduced x0.5 (premarket, 20261013-premarket-01-1)" in md
    # B's verdict is the one its decision rests on; without a decision, the day's latest
    assert "| taken R 2.60, +$10.12 | premarket allow 0.60 | taken |" in md
    assert "| n/a | vet neutral 0.50 |" not in md
    assert "|  | vet neutral 0.50 |  |" in md
    assert "| 20261013-vet-01-1 | vet | 09:05:00 | 3.00 | invalid_json | sonnet |  |  |" in md
    assert "| 20261013-vet-01-2 | vet | 09:05:00 | 16.50 | ok | sonnet | $0.14 | 1 |" in md
    assert "smoke" not in md
    # the model's text sits in a code span, longer than its own backticks: no link (a bare
    # URL included), image, HTML or emphasis; in a table its pipes are escaped too
    span = "``Ignore this | [click](http://evil.example) <img src=x onerror=alert(1)> `code` next``"
    assert f"- The day mode's reason: {span}" in md
    assert f"- The review (ok): {span}" in md
    cell = span.replace("|", "\\|")
    assert f"| XYZ | {cell} |  | `a \\| b` | good_process |" in md
    page = render_html(report)
    assert "<img" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page


async def test_the_issues_and_notes_the_review_raised_are_reported(journal: Journal) -> None:
    """A§4.10: new or recurring, each with today's text, escaped as the model's."""
    await llm_day(journal)
    statements = [
        "INSERT INTO process_issues (id, first_seen, last_seen, occurrences, severity, component, "
        "description, status) VALUES (4, '2026-10-01', '2026-10-13', 3, 'warn', 'vet', 'first', "
        "'open'), (9, '2026-10-13', '2026-10-13', 1, 'error', 'gate', 'brand new', 'open')",
        "INSERT INTO review_notes (id, first_raised, last_raised, times_raised, note, status) "
        "VALUES (2, '2026-10-13', '2026-10-13', 1, 'a note', 'open')",
        "INSERT INTO review_mentions (date, kind, item_id, severity, component, text) VALUES "
        "('2026-10-13', 'issue', 4, 'warn', 'vet', 'slow again | [x](http://e.x)'), "
        "('2026-10-13', 'issue', 9, 'error', 'gate', 'brand new'), "
        "('2026-10-13', 'note', 2, NULL, NULL, 'a note'), "
        "('2026-10-12', 'issue', 4, 'warn', 'vet', 'yesterday: not today')",
    ]
    for sql in statements:
        await journal.execute(sql)
    report = await build_daily(journal, DAY, books=("A", "B"), mode="paper", session=None, now=NOW)
    md = render_markdown(report)
    assert "process issues and notes (`trader followups list`)" in md
    assert "| issue | 4 | recurring (x3) | warn | `slow again \\| [x](http://e.x)` |" in md
    assert "| issue | 9 | new | error | `brand new` |" in md
    assert "| note | 2 | new |  | `a note` |" in md
    assert "yesterday" not in md


async def test_a_review_that_did_not_come_says_why(journal: Journal) -> None:
    await llm_day(journal)
    await journal.execute("UPDATE daily_reviews SET status = 'missing', summary = NULL, "
                          "failure = 'timeout'")  # fmt: skip
    report = await build_daily(journal, DAY, books=("A", "B"), mode="paper", session=None, now=NOW)
    assert "- The review (missing): timeout" in render_markdown(report)
    assert "<li>The review (missing): timeout</li>" in render_html(report)


async def test_a_day_book_b_was_kept_off_says_why(journal: Journal) -> None:
    report = await build_daily(journal, DAY, books=("A",), mode="paper", session=None, now=NOW,
                               book_b_off="--no-llm")  # fmt: skip
    assert "- LLM calls: none (Book B off: --no-llm)" in render_markdown(report)


def test_cli_regenerates_a_report_offline(repo: Path) -> None:
    db = open_database(repo / "runtime" / "trader.db")
    db.close()
    result = CliRunner().invoke(
        app, ["--root", str(repo), "report", "daily", "--date", "2026-10-13"]
    )
    assert result.exit_code == 0, result.output
    assert "2026-10-13 | A: +$0.00 (0 trades, 0 won)" in result.output
    assert (repo / "runtime" / "reports" / "2026-10-13.html").exists()
