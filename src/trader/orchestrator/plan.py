"""The day's timeline (A§3; DECISIONS D-048, D-069): every time the orchestrator acts on, for one
session.

Morning times are fixed clock times (the open is always 09:30), Book B's included: the 09:00
pre-market scan, the 09:05 brief and its 09:28 deadline. After the close they are offsets: EOD,
shadow outcomes, the review and the report run ``schedule.<time> - 16:00`` after the session's
close, so on an early close (13:00) they move to 13:05, 13:12, 13:15 and 13:30 (A§3). The review's
cutoff is a minute before the report (the Phase 4 plan's decision 5).
"""

from datetime import date, datetime, timedelta

from pydantic import BaseModel, ConfigDict

from trader.config import RiskConfig, Settings
from trader.core.calendar import DayTimes, TradingCalendar, day_times
from trader.core.clock import REGULAR_CLOSE, et_datetime

SUBSCRIBE_BEFORE_OPEN = timedelta(minutes=1)  # 09:29: the regime symbols start streaming
REVIEW_BEFORE_REPORT = timedelta(minutes=1)  # the review's cutoff (plan decision 5)


class DayPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    day: date
    times: DayTimes  # open, close, entry start, last entry, flatten, flat-by
    subscribe_at: datetime
    scan_at: datetime
    scan_deadline: datetime
    eod_at: datetime
    shadow_at: datetime
    report_at: datetime
    next_open: datetime
    # Book B (S§5.2, S§6.1)
    premarket_scan_at: datetime
    brief_at: datetime
    brief_deadline: datetime
    review_at: datetime
    review_cutoff: datetime

    @property
    def early_close(self) -> bool:
        return self.times.close.time() < REGULAR_CLOSE

    def lines(self, *, book_b: bool = False) -> list[str]:
        """The timeline, for ``run-day --dry-run``; Book B's rows only when it runs."""
        t = self.times
        rows = [
            ("subscribe regime symbols", self.subscribe_at),
            ("open", t.open),
            ("opening-range scan", self.scan_at),
            ("scan deadline", self.scan_deadline),
            ("entries from", t.entry_start),
            ("last entry", t.last_entry),
            ("flatten", t.flatten),
            ("flat by", t.flat_by),
            ("close", t.close),
            ("EOD bookkeeping", self.eod_at),
            ("shadow outcomes", self.shadow_at),
            ("report", self.report_at),
        ]
        if book_b:
            for row in (
                ("pre-market scan (Book B)", self.premarket_scan_at),
                ("Claude's pre-market brief (Book B)", self.brief_at),
                ("the brief's deadline", self.brief_deadline),
                ("Claude's post-close review (Book B)", self.review_at),
                ("the review's cutoff", self.review_cutoff),
            ):
                later = [i for i, (_, when) in enumerate(rows) if when > row[1]]
                rows.insert(later[0] if later else len(rows), row)
        head = f"session {self.day}" + (" (early close)" if self.early_close else "")
        return [head, *(f"  {when:%H:%M:%S}  {what}" for what, when in rows)]


def day_plan(calendar: TradingCalendar, day: date, settings: Settings, risk: RiskConfig) -> DayPlan:
    """The plan for ``day``. Raises NotATradingDayError when the market is closed."""
    session = calendar.session(day)
    schedule, selection = settings.schedule, settings.strategy.selection
    times = day_times(
        session,
        entry_window_start=risk.entry_window_start,
        last_entry=schedule.last_entry,
        flatten=schedule.flatten,
    )
    regular_close = et_datetime(day, REGULAR_CLOSE)

    def after_close(configured: datetime) -> datetime:
        return session.close + max(configured - regular_close, timedelta(0))

    report_at = after_close(et_datetime(day, schedule.reports))
    return DayPlan(
        day=day,
        times=times,
        subscribe_at=session.open - SUBSCRIBE_BEFORE_OPEN,
        scan_at=et_datetime(day, selection.scan_at),
        scan_deadline=et_datetime(day, selection.scan_deadline),
        eod_at=after_close(et_datetime(day, schedule.eod_reconcile)),
        shadow_at=after_close(et_datetime(day, schedule.shadow_outcomes)),
        report_at=report_at,
        next_open=calendar.session(calendar.next_session(day)).open,
        premarket_scan_at=et_datetime(day, schedule.premarket_scan),
        brief_at=et_datetime(day, schedule.premarket_brief),
        brief_deadline=et_datetime(day, schedule.premarket_brief_deadline),
        review_at=after_close(et_datetime(day, schedule.postclose_review)),
        review_cutoff=report_at - REVIEW_BEFORE_REPORT,
    )
