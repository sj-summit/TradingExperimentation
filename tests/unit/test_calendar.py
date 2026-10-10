from collections.abc import Mapping, Sequence
from datetime import UTC, date, time
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures import nyse
from trader.core.calendar import (
    CalendarFetcher,
    CalendarRangeError,
    NotATradingDayError,
    TradingCalendar,
    default_sync_range,
    load_calendar,
    parse_alpaca_calendar,
    sync_calendar,
)
from trader.memory.journal import Journal

D = date.fromisoformat


@pytest.fixture(scope="module")
def cal() -> TradingCalendar:
    return TradingCalendar(parse_alpaca_calendar(nyse.calendar_rows()), nyse.FIRST, nyse.LAST)


def utc_hhmm(cal: TradingCalendar, day: str, which: str) -> str:
    session = cal.session(D(day))
    stamp = session.open if which == "open" else session.close
    return stamp.astimezone(UTC).strftime("%H:%M")


# ---------------------------------------------------------------- sessions


@pytest.mark.parametrize(
    ("day", "open_utc"),
    [
        ("2026-03-06", "14:30"),  # Friday before the 2026-03-08 switch: EST
        ("2026-03-09", "13:30"),  # Monday after: EDT
        ("2026-10-30", "13:30"),  # Friday before the 2026-11-01 switch: EDT
        ("2026-11-02", "14:30"),  # Monday after: EST
    ],
)
def test_session_times_follow_dst(cal: TradingCalendar, day: str, open_utc: str) -> None:
    assert utc_hhmm(cal, day, "open") == open_utc
    assert cal.session(D(day)).open.time() == time(9, 30)


@pytest.mark.parametrize("day", ["2026-11-27", "2026-12-24", "2025-07-03"])
def test_early_closes_at_13(cal: TradingCalendar, day: str) -> None:
    session = cal.session(D(day))
    assert session.is_early_close
    assert session.close.time() == time(13, 0)


def test_regular_day_is_not_early(cal: TradingCalendar) -> None:
    session = cal.session(D("2026-07-02"))  # the day before the observed holiday: a full day
    assert not session.is_early_close
    assert session.close.time() == time(16, 0)


@pytest.mark.parametrize(
    ("day", "trading"),
    [
        ("2026-07-03", False),  # Independence Day observed (July 4 is a Saturday)
        ("2026-11-26", False),  # Thanksgiving
        ("2026-12-25", False),  # Christmas
        ("2026-11-28", False),  # Saturday
        ("2026-10-12", True),  # Columbus Day: NYSE open, banks closed
        ("2025-01-09", False),  # unscheduled closure (National Day of Mourning)
    ],
)
def test_is_trading_day(cal: TradingCalendar, day: str, trading: bool) -> None:
    assert cal.is_trading_day(D(day)) is trading


def test_session_on_a_holiday_raises(cal: TradingCalendar) -> None:
    with pytest.raises(NotATradingDayError):
        cal.session(D("2026-11-26"))


# ---------------------------------------------------------------- trading-day arithmetic


@pytest.mark.parametrize(
    ("start", "n", "expected"),
    [
        ("2026-11-25", 1, "2026-11-27"),  # over Thanksgiving
        ("2026-11-27", 1, "2026-11-30"),  # over a weekend
        ("2026-12-23", 2, "2026-12-28"),  # over Christmas and a weekend
        ("2026-11-30", -2, "2026-11-25"),  # backwards over a weekend and Thanksgiving
        ("2026-11-28", 1, "2026-11-30"),  # from a Saturday
        ("2026-11-28", -1, "2026-11-27"),
        ("2026-07-02", 1, "2026-07-06"),  # over the observed holiday
        ("2026-11-30", 0, "2026-11-30"),
    ],
)
def test_add_trading_days(cal: TradingCalendar, start: str, n: int, expected: str) -> None:
    assert cal.add_trading_days(D(start), n) == D(expected)


def test_add_zero_trading_days_from_a_holiday_raises(cal: TradingCalendar) -> None:
    with pytest.raises(NotATradingDayError):
        cal.add_trading_days(D("2026-11-26"), 0)


def test_next_and_previous_session(cal: TradingCalendar) -> None:
    assert cal.next_session(D("2026-12-24")) == D("2026-12-28")
    assert cal.previous_session(D("2026-11-27")) == D("2026-11-25")
    assert cal.previous_session(D("2026-11-28")) == D("2026-11-27")


def test_last_sessions_are_oldest_first(cal: TradingCalendar) -> None:
    expected = [D("2026-12-22"), D("2026-12-23"), D("2026-12-24")]
    assert cal.last_sessions(D("2026-12-28"), 3) == expected


def test_sessions_between_is_inclusive(cal: TradingCalendar) -> None:
    days = [s.date for s in cal.sessions_between(D("2026-11-25"), D("2026-11-30"))]
    assert days == [D("2026-11-25"), D("2026-11-27"), D("2026-11-30")]


def test_settlement_skips_bank_holidays(cal: TradingCalendar) -> None:
    # Friday 2026-10-09 settles Tuesday: Monday is Columbus Day (NYSE open, banks closed).
    assert cal.settlement_date(D("2026-10-09")) == D("2026-10-13")
    assert cal.add_trading_days(D("2026-10-09"), 1) == D("2026-10-12")


# ---------------------------------------------------------------- failing closed


def test_dates_outside_the_cached_range_raise(cal: TradingCalendar) -> None:
    for query in (
        lambda: cal.is_trading_day(D("2024-12-31")),
        lambda: cal.session(D("2028-01-03")),
        lambda: cal.next_session(D("2027-12-31")),  # no cached session after it
        lambda: cal.previous_session(D("2025-01-02")),  # first cached session
        lambda: cal.add_trading_days(D("2025-01-03"), -5),
        lambda: cal.last_sessions(D("2025-01-06"), 10),
    ):
        with pytest.raises(CalendarRangeError):
            query()


def test_sessions_outside_the_declared_range_are_rejected() -> None:
    sessions = parse_alpaca_calendar(nyse.calendar_rows(D("2026-01-02"), D("2026-01-09")))
    with pytest.raises(ValueError, match="outside"):
        TradingCalendar(sessions, D("2026-01-05"), D("2026-01-09"))


def test_default_sync_range() -> None:
    assert default_sync_range(D("2026-10-09")) == (D("2024-10-09"), D("2027-10-09"))


def test_parse_rejects_close_before_open() -> None:
    row = {"date": "2026-10-09", "open": "16:00", "close": "09:30", "settlement_date": "2026-10-13"}
    with pytest.raises(ValueError, match="close"):
        parse_alpaca_calendar([row])


# ---------------------------------------------------------------- sync and cache


def fetcher(rows: list[dict[str, str]]) -> CalendarFetcher:
    """Stands in for AlpacaRest.calendar: the rows inside [start, end]."""

    async def fetch(start: date, end: date) -> Sequence[Mapping[str, Any]]:
        return [r for r in rows if start <= D(r["date"]) <= end]

    return fetch


async def test_sync_persists_and_reloads(tmp_path: Path) -> None:
    first, last = D("2026-01-01"), D("2026-12-31")
    async with await Journal.open(tmp_path / "trader.db") as journal:
        cal, report = await sync_calendar(fetcher(nyse.calendar_rows()), journal, first, last)
        assert report.sessions == len(cal.sessions_between(first, last)) == 251
        assert report.removed == []
        assert report.settlement_differs == 2  # Columbus Day and Veterans Day eves
        reloaded = await load_calendar(journal)
    assert reloaded.sessions_between(first, last) == cal.sessions_between(first, last)
    assert (reloaded.first, reloaded.last) == (first, last)


async def test_resync_removes_cancelled_days_and_keeps_run_status(tmp_path: Path) -> None:
    first, last = D("2025-01-01"), D("2025-01-31")
    before_notice = nyse.CLOSED - {D("2025-01-09")}  # the closure was announced on 2024-12-30
    async with await Journal.open(tmp_path / "trader.db") as journal:
        early = fetcher(nyse.calendar_rows(first, last, closed=before_notice))
        cal, _ = await sync_calendar(early, journal, first, last)
        assert cal.is_trading_day(D("2025-01-09"))
        await journal.execute(
            "UPDATE trading_days SET run_status = 'done' WHERE date = '2025-01-08'"
        )
        cal, report = await sync_calendar(fetcher(nyse.calendar_rows()), journal, first, last)
        assert report.removed == [D("2025-01-09")]
        assert not cal.is_trading_day(D("2025-01-09"))
        row = await journal.fetch_one(
            "SELECT run_status FROM trading_days WHERE date = '2025-01-08'"
        )
    assert row is not None
    assert row["run_status"] == "done"


async def test_removing_a_day_that_was_already_run_is_refused(tmp_path: Path) -> None:
    first, last = D("2025-01-01"), D("2025-01-31")
    before_notice = nyse.CLOSED - {D("2025-01-09")}
    async with await Journal.open(tmp_path / "trader.db") as journal:
        early = fetcher(nyse.calendar_rows(first, last, closed=before_notice))
        await sync_calendar(early, journal, first, last)
        await journal.execute(
            "UPDATE trading_days SET run_status = 'done' WHERE date = '2025-01-09'"
        )
        with pytest.raises(RuntimeError, match="2025-01-09"):
            await sync_calendar(fetcher(nyse.calendar_rows()), journal, first, last)


async def test_loading_an_unsynced_calendar_raises(tmp_path: Path) -> None:
    async with await Journal.open(tmp_path / "trader.db") as journal:
        with pytest.raises(CalendarRangeError, match="never synced"):
            await load_calendar(journal)
