"""Book B's standing verdict on a symbol (S§6.5; D-071, D-073): the verdict its decision rests
on, else the day's latest. One rule for the review, the reports, the stats and the lessons.

- **Rests on:** Book B's final decision on the day's signal for the symbol names the call whose
  verdict it used (``final_decisions.llm_call_id``). That call's verdict stands.
- **Else the day's latest:** with no such decision (the signal never triggered, or its last
  decision names no call, such as an EOD ``entry_unfilled``), the latest verdict of the day
  stands, whichever call gave it.

Only trading calls count: a smoke run's verdicts are never Book B's. Verdicts join their signal
by date and symbol, since the brief's carry no ``signal_id``.
"""

import sqlite3
from datetime import date


def standing_verdicts(
    conn: sqlite3.Connection, *, first: date, last: date
) -> dict[tuple[str, str], sqlite3.Row]:
    """``(date, symbol)`` -> the standing verdict's row for each day from ``first`` through
    ``last``: the ``verdicts`` columns plus the call's ``logical_id``."""
    span = (first.isoformat(), last.isoformat())
    rests = {
        (r["date"], r["symbol"]): r["llm_call_id"]
        for r in conn.execute(
            "SELECT s.date, s.symbol, f.llm_call_id FROM final_decisions f JOIN signals s "
            "ON s.id = f.signal_id WHERE s.date BETWEEN ? AND ? AND f.book = 'B' "
            "AND f.llm_call_id IS NOT NULL",
            span,
        )
    }
    out: dict[tuple[str, str], sqlite3.Row] = {}
    chosen: set[tuple[str, str]] = set()
    for r in conn.execute(
        "SELECT v.*, c.logical_id FROM verdicts v JOIN llm_calls c ON c.id = v.llm_call_id "
        "WHERE v.date BETWEEN ? AND ? AND c.purpose = 'trading' ORDER BY v.date, v.id",
        span,
    ):
        key = (r["date"], r["symbol"])
        if key in chosen:
            continue
        out[key] = r  # oldest first: the latest wins, unless the decision named one
        if rests.get(key) == r["llm_call_id"]:
            chosen.add(key)
    return out


def standing_for_day(conn: sqlite3.Connection, day: date) -> dict[str, sqlite3.Row]:
    """The day's standing verdicts by symbol."""
    return {
        symbol: row for (_, symbol), row in standing_verdicts(conn, first=day, last=day).items()
    }
