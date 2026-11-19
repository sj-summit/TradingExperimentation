"""The day's timeline and the single-engine lock (P3.5; DECISIONS D-048)."""

from datetime import date, datetime, time
from pathlib import Path

import pytest

from tests.fixtures.exec_harness import CALENDAR, RISK, SETTINGS
from trader.core.calendar import NotATradingDayError
from trader.core.clock import ET
from trader.orchestrator.lock import RunLock, RunLockError
from trader.orchestrator.plan import day_plan


def at(d: date, h: int, m: int, s: int = 0) -> datetime:
    return datetime.combine(d, time(h, m, s), ET)


def test_a_normal_session() -> None:
    d = date(2026, 10, 13)
    plan = day_plan(CALENDAR, d, SETTINGS, RISK)
    t = plan.times
    assert (plan.subscribe_at, t.open) == (at(d, 9, 29), at(d, 9, 30))
    assert (plan.scan_at, plan.scan_deadline) == (at(d, 9, 35, 3), at(d, 9, 36, 30))
    assert (t.entry_start, t.last_entry) == (at(d, 9, 35, 3), at(d, 15, 30))
    assert (t.flatten, t.flat_by, t.close) == (at(d, 15, 55), at(d, 15, 58), at(d, 16, 0))
    assert (plan.eod_at, plan.shadow_at, plan.report_at) == (
        at(d, 16, 5), at(d, 16, 12), at(d, 16, 30),
    )  # fmt: skip
    assert plan.next_open == at(date(2026, 10, 14), 9, 30)
    assert not plan.early_close


def test_an_early_close_moves_everything_after_the_open() -> None:
    d = date(2026, 11, 27)  # the day after Thanksgiving: 13:00 close
    plan = day_plan(CALENDAR, d, SETTINGS, RISK)
    t = plan.times
    assert (t.last_entry, t.flatten, t.flat_by) == (at(d, 12, 30), at(d, 12, 55), at(d, 12, 58))
    assert (plan.eod_at, plan.shadow_at, plan.report_at) == (
        at(d, 13, 5), at(d, 13, 12), at(d, 13, 30),
    )  # fmt: skip
    assert plan.scan_at == at(d, 9, 35, 3)
    assert plan.next_open == at(date(2026, 11, 30), 9, 30)
    assert plan.early_close
    assert plan.lines()[0] == "session 2026-11-27 (early close)"
    assert "  12:55:00  flatten" in plan.lines()


def test_book_b_times_on_a_normal_session() -> None:
    """P4.8: the 09:00 scan, the 09:05 brief and its 09:28 deadline, the 16:15 review and its
    cutoff a minute before the report (the Phase 4 plan's decision 5)."""
    d = date(2026, 10, 13)
    plan = day_plan(CALENDAR, d, SETTINGS, RISK)
    assert (plan.premarket_scan_at, plan.brief_at, plan.brief_deadline) == (
        at(d, 9, 0), at(d, 9, 5), at(d, 9, 28),
    )  # fmt: skip
    assert (plan.review_at, plan.review_cutoff) == (at(d, 16, 15), at(d, 16, 29))
    assert "  09:05:00  Claude's pre-market brief (Book B)" not in plan.lines()
    lines = plan.lines(book_b=True)
    for row in (
        "  09:00:00  pre-market scan (Book B)",
        "  09:05:00  Claude's pre-market brief (Book B)",
        "  09:28:00  the brief's deadline",
        "  16:15:00  Claude's post-close review (Book B)",
        "  16:29:00  the review's cutoff",
    ):
        assert row in lines
    # each B row sits before the first row that comes later; the others keep their order
    assert lines[1:4] == [
        "  09:00:00  pre-market scan (Book B)",
        "  09:05:00  Claude's pre-market brief (Book B)",
        "  09:28:00  the brief's deadline",
    ]
    assert lines[-4:] == [
        "  16:12:00  shadow outcomes",
        "  16:15:00  Claude's post-close review (Book B)",
        "  16:29:00  the review's cutoff",
        "  16:30:00  report",
    ]
    assert [line for line in lines if "(Book B)" not in line and "'s " not in line] == (
        plan.lines()
    )


def test_an_early_close_moves_the_review_not_the_brief() -> None:
    d = date(2026, 11, 27)  # 13:00 close
    plan = day_plan(CALENDAR, d, SETTINGS, RISK)
    assert (plan.premarket_scan_at, plan.brief_at, plan.brief_deadline) == (
        at(d, 9, 0), at(d, 9, 5), at(d, 9, 28),
    )  # fmt: skip
    assert (plan.review_at, plan.review_cutoff, plan.report_at) == (
        at(d, 13, 15), at(d, 13, 29), at(d, 13, 30),
    )  # fmt: skip


def test_a_holiday_has_no_plan() -> None:
    with pytest.raises(NotATradingDayError):
        day_plan(CALENDAR, date(2026, 11, 26), SETTINGS, RISK)


def test_only_one_engine_holds_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "run-day.lock"
    first, second = RunLock(path), RunLock(path)
    first.acquire()
    with pytest.raises(RunLockError, match="another `trader run-day` is running"):
        second.acquire()
    assert not second.held
    first.release()
    with second:
        assert second.held
    assert not second.held
    first.release()  # a second release is harmless
