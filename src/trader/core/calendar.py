"""Trading calendar (ARCHITECTURE.md §4.1): sessions, early closes, trading-day arithmetic.

Sessions come from Alpaca's calendar (GET /v2/calendar: Eastern wall-clock open/close plus a
settlement date) and are cached in the ``trading_days`` table. Any query outside the cached
range raises CalendarRangeError: the calendar fails closed instead of guessing.
"""

import bisect
import datetime as dt
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict

from trader.core.clock import REGULAR_CLOSE, et_datetime, fmt_iso, parse_et_iso
from trader.memory.journal import Journal

log = structlog.get_logger(__name__)

CalendarFetcher = Callable[[dt.date, dt.date], Awaitable[Sequence[Mapping[str, Any]]]]
"""Returns Alpaca v2 calendar rows for [start, end]; ``AlpacaRest.calendar`` fits."""

SYNC_BACK_DAYS = 730
SYNC_AHEAD_DAYS = 365


class CalendarRangeError(LookupError):
    """The date is outside the cached calendar range; sync the calendar first."""


class NotATradingDayError(LookupError):
    """The date is inside the cached range but the market is closed."""


class Session(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: dt.date
    open: dt.datetime  # tz-aware ET
    close: dt.datetime  # tz-aware ET
    settlement_date: dt.date | None = None

    @property
    def is_early_close(self) -> bool:
        return self.close.time() < REGULAR_CLOSE


class TradingCalendar:
    """Sessions for the closed range [first, last]; dates outside it raise CalendarRangeError."""

    def __init__(self, sessions: Iterable[Session], first: dt.date, last: dt.date) -> None:
        if first > last:
            raise ValueError(f"empty calendar range {first}..{last}")
        self.first, self.last = first, last
        self._sessions: dict[dt.date, Session] = {}
        for session in sessions:
            if not first <= session.date <= last:
                raise ValueError(f"session {session.date} is outside {first}..{last}")
            self._sessions[session.date] = session
        self._dates = sorted(self._sessions)

    def covers(self, d: dt.date) -> bool:
        return self.first <= d <= self.last

    def is_trading_day(self, d: dt.date) -> bool:
        self._require(d)
        return d in self._sessions

    def session(self, d: dt.date) -> Session:
        self._require(d)
        try:
            return self._sessions[d]
        except KeyError:
            raise NotATradingDayError(f"{d} is not a trading day") from None

    def next_session(self, d: dt.date) -> dt.date:
        """The first trading day strictly after ``d``."""
        return self.add_trading_days(d, 1)

    def previous_session(self, d: dt.date) -> dt.date:
        """The last trading day strictly before ``d``."""
        return self.add_trading_days(d, -1)

    def add_trading_days(self, d: dt.date, n: int) -> dt.date:
        """The n-th trading day after ``d`` (n > 0) or before it (n < 0).

        ``d`` itself need not be a trading day; n == 0 returns ``d`` only if it is one.
        """
        self._require(d)
        if n == 0:
            return self.session(d).date
        if n > 0:
            i = bisect.bisect_right(self._dates, d) + n - 1
        else:
            i = bisect.bisect_left(self._dates, d) + n
        if not 0 <= i < len(self._dates):
            raise CalendarRangeError(f"{d} {n:+d} trading days is outside the cached calendar")
        return self._dates[i]

    def last_sessions(self, before: dt.date, n: int) -> list[dt.date]:
        """The ``n`` trading days strictly before ``before``, oldest first."""
        self._require(before)
        i = bisect.bisect_left(self._dates, before)
        if i < n:
            raise CalendarRangeError(f"fewer than {n} cached sessions before {before}")
        return self._dates[i - n : i]

    def sessions_between(self, start: dt.date, end: dt.date) -> list[Session]:
        """Sessions with start <= date <= end, in order."""
        self._require(start)
        self._require(end)
        lo, hi = bisect.bisect_left(self._dates, start), bisect.bisect_right(self._dates, end)
        return [self._sessions[d] for d in self._dates[lo:hi]]

    def settlement_date(self, trade_date: dt.date) -> dt.date:
        """When a trade on ``trade_date`` settles, per Alpaca (skips bank holidays)."""
        settles = self.session(trade_date).settlement_date
        if settles is None:
            raise CalendarRangeError(f"no settlement date cached for {trade_date}")
        return settles

    def _require(self, d: dt.date) -> None:
        if not self.covers(d):
            raise CalendarRangeError(
                f"{d} is outside the cached calendar {self.first}..{self.last}"
            )


LAST_ENTRY_BEFORE_CLOSE = dt.timedelta(minutes=30)
FLATTEN_BEFORE_CLOSE = dt.timedelta(minutes=5)
FLAT_BY_BEFORE_CLOSE = dt.timedelta(minutes=2)


class DayTimes(BaseModel):
    """One session's trading times (A§3). On an early close, last entry and flatten move up."""

    model_config = ConfigDict(frozen=True)

    open: dt.datetime
    close: dt.datetime
    entry_start: dt.datetime
    last_entry: dt.datetime  # min(schedule last_entry, close - 30 min)
    flatten: dt.datetime  # min(schedule flatten, close - 5 min)
    flat_by: dt.datetime  # close - 2 min: every book must be flat (RISK.md §5)


def day_times(
    session: Session, *, entry_window_start: dt.time, last_entry: dt.time, flatten: dt.time
) -> DayTimes:
    """Apply A§3's early-close rules to the configured times for ``session``."""
    day = session.date
    return DayTimes(
        open=session.open,
        close=session.close,
        entry_start=et_datetime(day, entry_window_start),
        last_entry=min(et_datetime(day, last_entry), session.close - LAST_ENTRY_BEFORE_CLOSE),
        flatten=min(et_datetime(day, flatten), session.close - FLATTEN_BEFORE_CLOSE),
        flat_by=session.close - FLAT_BY_BEFORE_CLOSE,
    )


def parse_alpaca_calendar(rows: Iterable[Mapping[str, Any]]) -> list[Session]:
    """Alpaca v2 calendar rows ("date", "open" "09:30", "close" "16:00" in ET) -> sessions."""
    sessions = []
    for row in rows:
        day = dt.date.fromisoformat(row["date"])
        opens = et_datetime(day, dt.time.fromisoformat(row["open"]))
        closes = et_datetime(day, dt.time.fromisoformat(row["close"]))
        if closes <= opens:
            raise ValueError(f"{day}: close {row['close']} is not after open {row['open']}")
        settles = row.get("settlement_date")
        sessions.append(
            Session(
                date=day,
                open=opens,
                close=closes,
                settlement_date=dt.date.fromisoformat(settles) if settles else None,
            )
        )
    return sessions


def default_sync_range(today: dt.date) -> tuple[dt.date, dt.date]:
    return today - dt.timedelta(days=SYNC_BACK_DAYS), today + dt.timedelta(days=SYNC_AHEAD_DAYS)


class CalendarSync(BaseModel):
    """What a sync changed. ``settlement_differs`` counts sessions that don't settle on the next
    trading day (bank holidays); a jump would mean the settlement cycle changed."""

    first: dt.date
    last: dt.date
    sessions: int
    removed: list[dt.date]
    settlement_differs: int


async def sync_calendar(
    fetch: CalendarFetcher, journal: Journal, first: dt.date, last: dt.date
) -> tuple[TradingCalendar, CalendarSync]:
    """Fetch [first, last], replace the cached sessions in that range, and record the range.

    Days Alpaca no longer lists (an unscheduled closure) are deleted, unless the day already
    has a run status, which is refused as a data conflict. Run status and notes survive.
    """
    sessions = parse_alpaca_calendar(await fetch(first, last))
    calendar = TradingCalendar(sessions, first, last)

    def write(conn: sqlite3.Connection) -> list[dt.date]:
        listed = {s.date.isoformat() for s in sessions}
        cached = conn.execute(
            "SELECT date, run_status FROM trading_days WHERE date BETWEEN ? AND ?",
            (first.isoformat(), last.isoformat()),
        ).fetchall()
        removed = sorted(row["date"] for row in cached if row["date"] not in listed)
        already_run = [
            row["date"] for row in cached if row["date"] in removed and row["run_status"]
        ]
        if already_run:
            raise RuntimeError(f"Alpaca no longer lists sessions that already ran: {already_run}")
        conn.executemany("DELETE FROM trading_days WHERE date = ?", [(d,) for d in removed])
        conn.executemany(
            "INSERT INTO trading_days (date, open_ts, close_ts, is_early_close, settlement_date) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (date) DO UPDATE SET open_ts = excluded.open_ts, "
            "close_ts = excluded.close_ts, is_early_close = excluded.is_early_close, "
            "settlement_date = excluded.settlement_date",
            [
                (
                    s.date.isoformat(),
                    fmt_iso(s.open),
                    fmt_iso(s.close),
                    int(s.is_early_close),
                    s.settlement_date.isoformat() if s.settlement_date else None,
                )
                for s in sessions
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [("calendar_first", first.isoformat()), ("calendar_last", last.isoformat())],
        )
        return [dt.date.fromisoformat(d) for d in removed]

    removed = await journal.write(write)
    report = CalendarSync(
        first=first,
        last=last,
        sessions=len(sessions),
        removed=removed,
        settlement_differs=_settlement_differences(calendar, sessions),
    )
    log.info("calendar synced", **report.model_dump(mode="json", exclude={"removed"}),
             removed=[d.isoformat() for d in removed])  # fmt: skip
    return calendar, report


async def load_calendar(journal: Journal) -> TradingCalendar:
    """The cached calendar. Raises CalendarRangeError if it was never synced."""
    first, last = await journal.get_meta("calendar_first"), await journal.get_meta("calendar_last")
    if first is None or last is None:
        raise CalendarRangeError("the trading calendar was never synced")
    rows = await journal.fetch_all(
        "SELECT date, open_ts, close_ts, settlement_date FROM trading_days "
        "WHERE date BETWEEN ? AND ? AND open_ts IS NOT NULL ORDER BY date",
        (first, last),
    )
    sessions = [
        Session(
            date=dt.date.fromisoformat(row["date"]),
            open=parse_et_iso(row["open_ts"]),
            close=parse_et_iso(row["close_ts"]),
            settlement_date=(
                dt.date.fromisoformat(row["settlement_date"]) if row["settlement_date"] else None
            ),
        )
        for row in rows
    ]
    return TradingCalendar(sessions, dt.date.fromisoformat(first), dt.date.fromisoformat(last))


def _settlement_differences(calendar: TradingCalendar, sessions: Sequence[Session]) -> int:
    differs = 0
    for s in sessions:
        if s.settlement_date is None:
            continue
        try:
            if s.settlement_date != calendar.next_session(s.date):
                differs += 1
        except CalendarRangeError:
            continue  # the next session is beyond the synced range
    return differs
