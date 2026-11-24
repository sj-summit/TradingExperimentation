"""Unattended sessions: S§8 criterion 4 (the Phase 6 plan's decisions 6-8; DECISIONS D-081).

**A session is unattended** when the OS scheduler started it and nobody touched it:
- run-day ran with ``--scheduled``, which leaves a ``day_runs`` note ``scheduled``;
- it ran to DONE with exactly one BOOT, so it never restarted;
- there is no ``human`` row. ``trader halt``, ``flatten``, ``resume``, ``smoke paper`` and a run-day
  started by hand each write one, dated by the session they touch;
- no ``kill_switch`` halt was created: the KILL file is a person's act;
- run-day logged no ``scheduled_job_failed``;
- both books ran: A and B have ``daily_stats`` rows, and there is no ``book_b_off`` note.

A ``human`` row, a kill-switch halt or a P1 dated on a weekend or holiday counts against the next
session. Criterion 4 walks the epoch's calendar sessions, from its first with stats through the
memo's last day, so a session with no run breaks the streak.
"""

import bisect
import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from trader.core.clock import Clock, fmt_iso
from trader.memory.journal import Journal

BOOKS = ("A", "B")


@dataclass(frozen=True)
class SessionRun:
    day: date
    unattended: bool
    why_not: str = ""  # the first reason it isn't
    p1: int = 0

    @property
    def clean(self) -> bool:
        """Unattended and free of P1 incidents: what criterion 4 counts."""
        return self.unattended and not self.p1


def record_human(conn: sqlite3.Connection, *, at_iso: str, day: date, command: str) -> None:
    """A person's command touched ``day``'s session, inside the caller's transaction."""
    conn.execute(
        "INSERT INTO day_runs (date, kind, name, at) VALUES (?, 'human', ?, ?)",
        (day.isoformat(), command, at_iso),
    )


async def note_human(journal: Journal, clock: Clock, command: str) -> None:
    """Journal that ``command`` (``trader halt`` and the like) was run today."""
    now = clock.now()
    await journal.write(
        lambda conn: record_human(conn, at_iso=fmt_iso(now), day=now.date(), command=command)
    )


def calendar_sessions(conn: sqlite3.Connection, first: date, last: date) -> list[date]:
    """The trading sessions from ``first`` through ``last``, from the journal's calendar."""
    rows = conn.execute(
        "SELECT date FROM trading_days WHERE date BETWEEN ? AND ? ORDER BY date",
        (first.isoformat(), last.isoformat()),
    ).fetchall()
    return [date.fromisoformat(row[0]) for row in rows]


def epoch_sessions(conn: sqlite3.Connection, epoch: int, through: date) -> list[date]:
    """The epoch's calendar sessions, from its first session with stats through ``through``."""
    row = conn.execute(
        "SELECT MIN(date) FROM daily_stats WHERE eval_epoch = ? AND date <= ?",
        (epoch, through.isoformat()),
    ).fetchone()
    if row is None or row[0] is None:
        return []
    return calendar_sessions(conn, date.fromisoformat(row[0]), through)


def session_runs(conn: sqlite3.Connection, sessions: Sequence[date]) -> list[SessionRun]:
    """Each session's verdict, in order."""
    if not sessions:
        return []
    days = sorted(sessions)
    span = (days[0].isoformat(), days[-1].isoformat())

    def onto(day_text: str) -> date | None:
        """The session a date's event counts against: itself, or the next one."""
        found = bisect.bisect_left(days, date.fromisoformat(day_text[:10]))
        return days[found] if found < len(days) else None

    notes = {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT date, name FROM day_runs WHERE kind = 'note' AND date BETWEEN ? AND ?", span
        )
    }
    boots = Counter(
        r[0]
        for r in conn.execute(
            "SELECT date FROM day_runs WHERE kind = 'state' AND name = 'BOOT' "
            "AND date BETWEEN ? AND ?",
            span,
        )
    )
    status = dict(
        conn.execute(
            "SELECT date, run_status FROM trading_days WHERE date BETWEEN ? AND ?", span
        ).fetchall()
    )
    stats = {
        (r[0], r[1])
        for r in conn.execute("SELECT date, book FROM daily_stats WHERE date BETWEEN ? AND ?", span)
    }
    humans: dict[date, list[str]] = defaultdict(list)
    for day_text, name in conn.execute(
        "SELECT date, name FROM day_runs WHERE kind = 'human' AND date BETWEEN ? AND ? ORDER BY id",
        span,
    ):
        target = onto(day_text)
        if target is not None:
            humans[target].append(name)
    kills = {
        target
        for (created,) in conn.execute(
            "SELECT created_at FROM halts WHERE reason = 'kill_switch' AND created_at >= ? "
            "AND substr(created_at, 1, 10) <= ?",
            span,
        )
        if (target := onto(created)) is not None
    }
    failed: set[date] = set()
    for ts, detail in conn.execute(
        "SELECT ts, detail_json FROM incidents WHERE code = 'scheduled_job_failed' "
        "AND ts >= ? AND substr(ts, 1, 10) <= ?",
        span,
    ):
        if json.loads(detail or "{}").get("job") == "run-day" and (target := onto(ts)):
            failed.add(target)
    p1s: Counter[date] = Counter()
    for (ts,) in conn.execute(
        "SELECT ts FROM incidents WHERE severity = 'P1' AND ts >= ? AND substr(ts, 1, 10) <= ?",
        span,
    ):
        if (target := onto(ts)) is not None:
            p1s[target] += 1

    out: list[SessionRun] = []
    for day in days:
        key = day.isoformat()
        reasons = [
            ((key, "scheduled") not in notes, "not started by the scheduler"),
            (status.get(key) != "DONE", "didn't run to the end"),
            (boots[key] > 1, f"restarted ({boots[key]} boots)"),
            (bool(humans[day]), "touched by hand: " + ", ".join(dict.fromkeys(humans[day]))),
            (day in kills, "the kill switch"),
            (day in failed, "run-day failed and was retried"),
            ((key, "book_b_off") in notes, "Book B was off"),
            (any((key, b) not in stats for b in BOOKS), "a book has no daily stats"),
        ]
        why = next((text for hit, text in reasons if hit), "")
        out.append(SessionRun(day=day, unattended=not why, why_not=why, p1=p1s[day]))
    return out


def longest_clean_run(runs: Sequence[SessionRun]) -> int:
    """The most consecutive sessions both unattended and free of P1s."""
    best = streak = 0
    for run in runs:
        streak = streak + 1 if run.clean else 0
        best = max(best, streak)
    return best
