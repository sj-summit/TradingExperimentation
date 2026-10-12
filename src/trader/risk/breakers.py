"""Circuit breakers (RISK.md §5) and the per-book halts they set.

The breaker checks are pure. Halts are rows in the ``halts`` table, so they stack and clear one
at a time (DECISIONS D-036): a manual resume can never lift a daily-loss halt early.

Scopes:
- ``until_resume``: lifted only by ``trader resume`` (drawdown needs ``--ack-drawdown``);
- ``session``: lifted automatically when ``expires_at`` (the next session's open) passes;
- ``marker``: a short-lived flag such as ``flattening``, renewed while in use; if its owner
  dies it lapses on its own.

Data staleness is not a halt: it is an input to the gate (R-016) while it lasts.
``book_state.halted`` and ``halt_reason`` keep a summary, refreshed on every change.
"""

import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from trader.config import BookName
from trader.core.clock import Clock, fmt_iso, parse_et_iso
from trader.memory.journal import Journal, db_to_dec, dec_to_db
from trader.risk import caps

HUNDRED = Decimal(100)

HaltReason = Literal[
    "kill_switch",
    "drawdown",
    "losing_streak",
    "reconcile_mismatch",
    "cash_mismatch",
    "p1_incident",
    "manual",
    "daily_loss",
    "llm_degraded",
    "flattening",
]
HaltScope = Literal["until_resume", "session", "marker"]
SCOPE: dict[str, HaltScope] = {
    "kill_switch": "until_resume",
    "drawdown": "until_resume",
    "losing_streak": "until_resume",
    "reconcile_mismatch": "until_resume",
    "cash_mismatch": "until_resume",
    "p1_incident": "until_resume",
    "manual": "until_resume",
    "daily_loss": "session",
    "llm_degraded": "session",
    "flattening": "marker",
}
Staleness = Literal["ok", "warn", "halt"]


# ---------------------------------------------------------------- breaker checks (pure)


def daily_loss_breached(
    realized_pnl: Decimal, unrealized_pnl: Decimal, equity_sod: Decimal, limit_pct: Decimal
) -> bool:
    """Realized plus mark-to-market loss reached the daily limit (RISK.md §5)."""
    limit = min(limit_pct, caps.MAX_DAILY_LOSS_LIMIT_PCT) / HUNDRED * equity_sod
    return -(realized_pnl + unrealized_pnl) >= limit


def drawdown_pct(peak_equity: Decimal, equity: Decimal) -> Decimal:
    """Peak-to-trough drawdown in percent; zero without a positive peak."""
    if peak_equity <= 0:
        return Decimal(0)
    return max(Decimal(0), (peak_equity - equity) / peak_equity * HUNDRED)


def drawdown_breached(peak_equity: Decimal, equity: Decimal, breaker_pct: Decimal) -> bool:
    return drawdown_pct(peak_equity, equity) >= breaker_pct


def losing_streak_breached(consecutive_losing_days: int, limit: int) -> bool:
    return consecutive_losing_days >= limit


def staleness(age_s: float, warn_s: float, halt_s: float) -> Staleness:
    """No market data for ``warn_s`` warns; for ``halt_s`` it blocks new entries."""
    if age_s >= halt_s:
        return "halt"
    return "warn" if age_s >= warn_s else "ok"


# ---------------------------------------------------------------- halts


class Halt(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    book: str
    reason: str
    scope: HaltScope
    detail: str
    created_at: datetime
    expires_at: datetime | None


class HaltError(RuntimeError):
    """A resume or halt request that the rules refuse."""


def active_halts(conn: sqlite3.Connection, book: str, now: datetime) -> list[Halt]:
    """The book's halts in force at ``now``, inside the caller's transaction."""
    rows = conn.execute(
        "SELECT id, book, reason, scope, detail, created_at, expires_at FROM halts "
        "WHERE book = ? AND cleared_at IS NULL ORDER BY id",
        (book,),
    ).fetchall()
    halts = [_halt(row) for row in rows]
    return [h for h in halts if h.expires_at is None or h.expires_at > now]


class Halts:
    """The halts table for every book, through the journal."""

    def __init__(self, journal: Journal, clock: Clock) -> None:
        self._journal, self._clock = journal, clock

    async def halt(
        self,
        book: BookName,
        reason: HaltReason,
        *,
        detail: str = "",
        expires_at: datetime | None = None,
    ) -> Halt:
        """Put a halt in force; an identical one already in force is returned unchanged."""
        scope = SCOPE[reason]
        if scope != "until_resume" and expires_at is None:
            raise HaltError(f"a {scope} halt ({reason}) needs an expiry")
        now = self._clock.now()

        def write(conn: sqlite3.Connection) -> Halt:
            for existing in active_halts(conn, book, now):
                if existing.reason == reason:
                    if expires_at is not None and existing.expires_at != expires_at:
                        conn.execute(
                            "UPDATE halts SET expires_at = ? WHERE id = ?",
                            (fmt_iso(expires_at), existing.id),
                        )
                        _summarize(conn, book, now)
                        return existing.model_copy(update={"expires_at": expires_at})
                    return existing
            cursor = conn.execute(
                "INSERT INTO halts (book, reason, scope, detail, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (book, reason, scope, detail, fmt_iso(now), _ts(expires_at)),
            )
            _summarize(conn, book, now)
            return Halt(
                id=cursor.lastrowid or 0,
                book=book,
                reason=reason,
                scope=scope,
                detail=detail,
                created_at=now,
                expires_at=expires_at,
            )

        return await self._journal.write(write)

    async def active(self, book: BookName) -> list[Halt]:
        now = self._clock.now()
        rows = await self._journal.fetch_all(
            "SELECT id, book, reason, scope, detail, created_at, expires_at FROM halts "
            "WHERE book = ? AND cleared_at IS NULL ORDER BY id",
            (book,),
        )
        return [h for h in map(_halt, rows) if h.expires_at is None or h.expires_at > now]

    async def renew_marker(self, book: BookName, reason: HaltReason, ttl: timedelta) -> Halt:
        """Set or extend a marker so it lasts ``ttl`` from now."""
        return await self.halt(book, reason, expires_at=self._clock.now() + ttl)

    async def clear(self, book: BookName, reasons: Iterable[str], *, by: str) -> list[Halt]:
        """Clear the named halts that are in force; returns what was cleared."""
        wanted, now = set(reasons), self._clock.now()

        def write(conn: sqlite3.Connection) -> list[Halt]:
            cleared = [h for h in active_halts(conn, book, now) if h.reason in wanted]
            conn.executemany(
                "UPDATE halts SET cleared_at = ?, cleared_by = ? WHERE id = ?",
                [(fmt_iso(now), by, h.id) for h in cleared],
            )
            _summarize(conn, book, now)
            return cleared

        return await self._journal.write(write)

    async def resume(self, book: BookName, *, by: str, ack_drawdown: bool) -> list[Halt]:
        """Clear every halt that only a human may lift. Session halts and markers stay;
        a drawdown halt needs an explicit acknowledgement (RISK.md §5)."""
        lasting = [h for h in await self.active(book) if h.scope == "until_resume"]
        if any(h.reason == "drawdown" for h in lasting) and not ack_drawdown:
            raise HaltError("a drawdown halt needs --ack-drawdown")
        return await self.clear(book, {h.reason for h in lasting}, by=by)


def _summarize(conn: sqlite3.Connection, book: str, now: datetime) -> None:
    """Keep ``book_state``'s halted/halt_reason columns as a summary of the halts table."""
    lasting = [h for h in active_halts(conn, book, now) if h.scope != "marker"]
    conn.execute(
        "INSERT INTO book_state (book, halted, halt_reason, halted_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (book) DO UPDATE SET halted = excluded.halted, "
        "halt_reason = excluded.halt_reason, halted_at = excluded.halted_at, "
        "updated_at = excluded.updated_at",
        (
            book,
            int(bool(lasting)),
            ",".join(h.reason for h in lasting) or None,
            fmt_iso(lasting[0].created_at) if lasting else None,
            fmt_iso(now),
        ),
    )


def _halt(row: sqlite3.Row) -> Halt:
    return Halt(
        id=row["id"],
        book=row["book"],
        reason=row["reason"],
        scope=row["scope"],
        detail=row["detail"] or "",
        created_at=parse_et_iso(row["created_at"]),
        expires_at=parse_et_iso(row["expires_at"]) if row["expires_at"] else None,
    )


def _ts(value: datetime | None) -> str | None:
    return None if value is None else fmt_iso(value)


# ---------------------------------------------------------------- peak equity and streaks


class BookStats:
    """``book_state``'s peak equity and losing streak (the drawdown and streak breakers)."""

    def __init__(self, journal: Journal, clock: Clock) -> None:
        self._journal, self._clock = journal, clock

    async def peak_equity(self, book: BookName) -> Decimal | None:
        row = await self._journal.fetch_one(
            "SELECT peak_equity FROM book_state WHERE book = ?", (book,)
        )
        return None if row is None else db_to_dec(row["peak_equity"])

    async def raise_peak(self, book: BookName, equity: Decimal) -> Decimal:
        """Record ``equity`` if it is a new peak; returns the peak."""
        now = self._clock.now()

        def write(conn: sqlite3.Connection) -> Decimal:
            row = conn.execute(
                "SELECT peak_equity FROM book_state WHERE book = ?", (book,)
            ).fetchone()
            peak = db_to_dec(row["peak_equity"]) if row is not None else None
            if peak is not None and peak >= equity:
                return peak
            conn.execute(
                "INSERT INTO book_state (book, peak_equity, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (book) DO UPDATE SET peak_equity = excluded.peak_equity, "
                "updated_at = excluded.updated_at",
                (book, dec_to_db(equity), fmt_iso(now)),
            )
            return equity

        return await self._journal.write(write)

    async def record_day(self, book: BookName, day: date, pnl: Decimal, *, traded: bool) -> int:
        """Count consecutive losing sessions (RISK.md §5; DECISIONS D-048). A session that
        traded and lost extends the streak; any other session, one without trades included,
        ends it. Each session counts once: recording a day already counted (an EOD rerun)
        returns the streak unchanged."""
        now = self._clock.now()

        def write(conn: sqlite3.Connection) -> int:
            row = conn.execute(
                "SELECT consecutive_losing_days, streak_through FROM book_state WHERE book = ?",
                (book,),
            ).fetchone()
            streak = (row["consecutive_losing_days"] or 0) if row is not None else 0
            through = row["streak_through"] if row is not None else None
            if through is not None and through >= day.isoformat():
                return int(streak)
            streak = streak + 1 if traded and pnl < 0 else 0
            conn.execute(
                "INSERT INTO book_state (book, consecutive_losing_days, streak_through, "
                "updated_at) VALUES (?, ?, ?, ?) ON CONFLICT (book) DO UPDATE SET "
                "consecutive_losing_days = excluded.consecutive_losing_days, "
                "streak_through = excluded.streak_through, updated_at = excluded.updated_at",
                (book, streak, day.isoformat(), fmt_iso(now)),
            )
            return int(streak)

        return await self._journal.write(write)
