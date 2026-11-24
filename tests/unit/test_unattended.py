"""Unattended sessions, S§8 criterion 4 (P6.1b; DECISIONS D-081)."""

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import pytest

from tests.fixtures.simtime import JumpClock
from trader.core.clock import ET
from trader.memory.journal import Journal, open_database
from trader.memory.unattended import (
    SessionRun,
    calendar_sessions,
    epoch_sessions,
    longest_clean_run,
    note_human,
    session_runs,
)

FRI, MON, TUE = date(2026, 12, 4), date(2026, 12, 7), date(2026, 12, 8)
SAT, SUN = date(2026, 12, 5), date(2026, 12, 6)
SESSIONS = (date(2026, 12, 3), FRI, MON, TUE)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = open_database(tmp_path / "trader.db")
    db.isolation_level = None
    db.executemany(
        "INSERT INTO trading_days (date, open_ts, close_ts) VALUES (?, ?, ?)",
        [(d.isoformat(), f"{d}T09:30:00-05:00", f"{d}T16:00:00-05:00") for d in SESSIONS],
    )
    return db


def run(
    conn: sqlite3.Connection,
    day: date,
    *,
    scheduled: bool = True,
    boots: int = 1,
    done: bool = True,
    books: tuple[str, ...] = ("A", "B"),
) -> None:
    """A session as the engine leaves it."""
    key = day.isoformat()
    for _ in range(boots):
        conn.execute(
            "INSERT INTO day_runs (date, kind, name, at) VALUES (?, 'state', 'BOOT', ?)",
            (key, f"{key}T08:30:00-05:00"),
        )
    if scheduled:
        conn.execute(
            "INSERT INTO day_runs (date, kind, name, at) VALUES (?, 'note', 'scheduled', ?)",
            (key, f"{key}T08:25:01-05:00"),
        )
    if done:
        conn.execute("UPDATE trading_days SET run_status = 'DONE' WHERE date = ?", (key,))
    for book in books:
        conn.execute(
            "INSERT INTO daily_stats (date, book, pnl, eval_epoch) VALUES (?, ?, '0', 0)",
            (key, book),
        )


def human(conn: sqlite3.Connection, day: date, command: str = "trader halt") -> None:
    conn.execute(
        "INSERT INTO day_runs (date, kind, name, at) VALUES (?, 'human', ?, ?)",
        (day.isoformat(), command, f"{day}T12:00:00-05:00"),
    )


def incident(
    conn: sqlite3.Connection,
    day: date,
    severity: str,
    code: str,
    detail: dict[str, object] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO incidents (ts, severity, component, code, message, detail_json) "
        "VALUES (?, ?, 'x', ?, 'm', ?)",
        (f"{day}T11:00:00-05:00", severity, code, json.dumps(detail) if detail else None),
    )


def verdict(conn: sqlite3.Connection, day: date) -> SessionRun:
    return {r.day: r for r in session_runs(conn, SESSIONS)}[day]


def test_a_scheduled_day_nobody_touched(conn: sqlite3.Connection) -> None:
    run(conn, FRI)
    found = verdict(conn, FRI)
    assert found == SessionRun(day=FRI, unattended=True, why_not="", p1=0)
    assert found.clean


def _halted(c: sqlite3.Connection) -> None:
    run(c, FRI)
    human(c, FRI)


def _kill_file(c: sqlite3.Connection) -> None:
    run(c, FRI)
    c.execute(
        "INSERT INTO halts (book, reason, scope, created_at) "
        "VALUES ('A', 'kill_switch', 'until_resume', '2026-12-04T10:00:00-05:00')"
    )


def _retried(c: sqlite3.Connection) -> None:
    run(c, FRI)
    incident(c, FRI, "P2", "scheduled_job_failed", {"job": "run-day"})


def _book_b_off(c: sqlite3.Connection) -> None:
    run(c, FRI, books=("A",))
    c.execute(
        "INSERT INTO day_runs (date, kind, name, at) "
        "VALUES ('2026-12-04', 'note', 'book_b_off', '2026-12-04T08:31:00-05:00')"
    )


DISQUALIFIERS: list[tuple[str, Callable[[sqlite3.Connection], None], str]] = [
    ("manual", lambda c: run(c, FRI, scheduled=False), "not started by the scheduler"),
    ("unfinished", lambda c: run(c, FRI, done=False), "didn't run to the end"),
    ("restarted", lambda c: run(c, FRI, boots=2), "restarted (2 boots)"),
    ("halted", _halted, "touched by hand: trader halt"),
    ("kill file", _kill_file, "the kill switch"),
    ("retried", _retried, "run-day failed and was retried"),
    ("book b off", _book_b_off, "Book B was off"),
    ("no stats", lambda c: run(c, FRI, books=("A",)), "a book has no daily stats"),
]


@pytest.mark.parametrize(("_name", "make", "why"), DISQUALIFIERS, ids=[d[0] for d in DISQUALIFIERS])
def test_what_makes_a_session_attended(
    conn: sqlite3.Connection, _name: str, make: Callable[[sqlite3.Connection], None], why: str
) -> None:
    make(conn)
    found = verdict(conn, FRI)
    assert (found.unattended, found.why_not) == (False, why)
    assert not found.clean


def test_another_jobs_failure_doesnt_count(conn: sqlite3.Connection) -> None:
    run(conn, FRI)
    incident(conn, FRI, "P2", "scheduled_job_failed", {"job": "weekly-job"})
    assert verdict(conn, FRI).unattended


def test_a_p1_leaves_it_unattended_but_not_clean(conn: sqlite3.Connection) -> None:
    run(conn, FRI)
    incident(conn, FRI, "P1", "unprotected_position")
    incident(conn, FRI, "P2", "data_stale")
    found = verdict(conn, FRI)
    assert (found.unattended, found.p1, found.clean) == (True, 1, False)


def test_weekend_events_count_against_the_next_session(conn: sqlite3.Connection) -> None:
    for day in SESSIONS:
        run(conn, day)
    human(conn, SAT, "trader resume")
    incident(conn, SUN, "P1", "not_flat")
    found = {r.day: r for r in session_runs(conn, SESSIONS)}
    assert (found[FRI].clean, found[TUE].clean) == (True, True)
    assert (found[MON].why_not, found[MON].p1) == ("touched by hand: trader resume", 1)


def test_a_session_without_a_run_breaks_the_streak(conn: sqlite3.Connection) -> None:
    for day in (date(2026, 12, 3), FRI, TUE):
        run(conn, day)  # Monday never ran
    runs = session_runs(conn, SESSIONS)
    assert [r.clean for r in runs] == [True, True, False, True]
    assert longest_clean_run(runs) == 2
    assert longest_clean_run([]) == 0


def test_calendar_and_epoch_sessions(conn: sqlite3.Connection) -> None:
    assert calendar_sessions(conn, FRI, MON) == [FRI, MON]  # no weekend days
    assert epoch_sessions(conn, 0, TUE) == []  # no stats yet
    run(conn, FRI)
    assert epoch_sessions(conn, 0, TUE) == [FRI, MON, TUE]
    assert epoch_sessions(conn, 0, MON) == [FRI, MON]
    assert epoch_sessions(conn, 1, TUE) == []


def test_note_human(tmp_path: Path) -> None:
    async def go() -> list[tuple[str, str, str]]:
        clock = JumpClock(datetime(2026, 12, 5, 11, 0, tzinfo=ET))
        async with await Journal.open(tmp_path / "trader.db") as journal:
            await note_human(journal, clock, "trader flatten")
            rows = await journal.fetch_all("SELECT date, kind, name FROM day_runs")
        return [tuple(r) for r in rows]

    assert asyncio.run(go()) == [("2026-12-05", "human", "trader flatten")]
