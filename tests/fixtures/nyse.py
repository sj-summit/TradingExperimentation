"""Alpaca-format calendar rows (GET /v2/calendar) built from the official NYSE schedules.

Sources: NYSE Group holiday and early-closing calendars for 2025-2027 (ICE press releases of
2024-11-08 and 2025-12-23), the unscheduled 2025-01-09 National Day of Mourning closure, and
https://www.nyse.com/markets/hours-calendars. Settlement dates model DTC: the next NYSE trading
day that is not also a bank holiday (Columbus Day, Veterans Day), mirroring Alpaca's
``settlement_date``.
"""

from datetime import date, timedelta

CLOSED = frozenset(
    date.fromisoformat(d)
    for d in (
        # 2025 (01-09: National Day of Mourning, announced 2024-12-30)
        "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
        "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
        # 2026 (07-03: Independence Day observed)
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
        "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
        # 2027 (observed: 06-18 Juneteenth, 07-05 Independence Day, 12-24 Christmas)
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
        "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    )
)  # fmt: skip

EARLY_CLOSE = frozenset(
    date.fromisoformat(d)
    for d in ("2025-07-03", "2025-11-28", "2025-12-24", "2026-11-27", "2026-12-24", "2027-11-26")
)

# NYSE trades, but banks and DTC don't settle (Columbus Day, Veterans Day).
BANK_HOLIDAYS = frozenset(
    date.fromisoformat(d)
    for d in ("2025-10-13", "2025-11-11", "2026-10-12", "2026-11-11", "2027-10-11", "2027-11-11")
)

FIRST, LAST = date(2025, 1, 1), date(2027, 12, 31)


def is_open(d: date) -> bool:
    return d.weekday() < 5 and d not in CLOSED


def settlement(d: date) -> date:
    nxt = d + timedelta(days=1)
    while not is_open(nxt) or nxt in BANK_HOLIDAYS:
        nxt += timedelta(days=1)
    return nxt


def calendar_rows(
    start: date = FIRST, end: date = LAST, *, closed: frozenset[date] = CLOSED
) -> list[dict[str, str]]:
    """One row per trading day in [start, end], exactly as Alpaca's v2 calendar returns them."""
    rows = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in closed:
            rows.append(
                {
                    "date": d.isoformat(),
                    "open": "09:30",
                    "close": "13:00" if d in EARLY_CLOSE else "16:00",
                    "session_open": "0400",
                    "session_close": "2000",
                    "settlement_date": settlement(d).isoformat(),
                }
            )
        d += timedelta(days=1)
    return rows
