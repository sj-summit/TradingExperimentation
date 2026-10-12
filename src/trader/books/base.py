"""Books (A§4.4; DECISIONS D-044): what a book does with a trigger, and the decision log.

Decisions are append-only. A row is written when a signal triggers (or is missed at arming);
an outcome row is appended later only when it says something new: ``broker_rejected`` at once,
``entry_unfilled`` at EOD for an entry that never filled, ``restart_lost`` after a crash. The
``final_decisions`` view is the latest row per signal and book, which reports and stats read.
"""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from trader.books.reasons import reason_from_violations
from trader.config import BookName
from trader.core.clock import fmt_iso
from trader.core.models import Asset, OrderIntent, Signal, TriggerEvent
from trader.execution.broker import is_final
from trader.execution.records import LOST
from trader.execution.service import EntryResult, GateStateBuilder
from trader.memory.journal import Journal


@dataclass(frozen=True)
class BookDecision:
    book: BookName
    signal_id: str
    take: bool
    reason: str  # a ReasonCode, or risk_gate:R-xxx
    detail: str = ""
    intent: OrderIntent | None = None
    entry: EntryResult | None = None


@dataclass(frozen=True)
class PreparedInputs:
    """Everything the gate needs that takes the network, gathered before the book's lock, so
    the gate-state builder only assembles values (resolution 4 of the Phase 3 plan). A value
    that couldn't be had is None, which makes its rule reject (R-016, R-019)."""

    last_price: Decimal | None
    quote_age_s: float | None
    broker_equity: Decimal | None
    asset: Asset | None
    corporate_action: bool = False
    hub_stale: bool = False
    symbol_halted: bool | None = None
    halts: tuple[str, ...] = ()  # the book's halts in force, and kill_switch if the file exists


class EntryInputs(Protocol):
    """Gathers the gate's inputs (implemented by the orchestrator)."""

    async def prepare(self, book: BookName, sig: Signal, ev: TriggerEvent) -> PreparedInputs: ...

    def builder(
        self, book: BookName, sig: Signal, prepared: PreparedInputs
    ) -> GateStateBuilder: ...


class Book(Protocol):
    name: BookName

    async def on_trigger(self, ev: TriggerEvent, sig: Signal) -> BookDecision: ...


def decision_for(
    book: BookName, sig: Signal, intent: OrderIntent, result: EntryResult, detail: str
) -> BookDecision:
    """Map ExecutionService's outcome to a reason code (A§4.4, D-037)."""
    reason, take = "taken", True
    if result.outcome == "uncertain":
        detail = f"submit_uncertain; {detail}"
    elif result.outcome == "rejected":
        violations = result.check.violations if result.check is not None else ()
        reason, take = reason_from_violations(violations), False
        detail = f"{', '.join(violations)}; {detail}"
    elif result.outcome == "broker_rejected":
        reason, take = "broker_rejected", False
        detail = f"{result.reason}; {detail}"
    elif result.outcome == "halted":
        # A service that flattened takes no more entries that day, even after a resume.
        reason, take = "book_halted", False
        detail = f"{result.reason}; {detail}"
    return BookDecision(book=book, signal_id=sig.id, take=take, reason=reason, detail=detail,
                        intent=intent, entry=result)  # fmt: skip


def insert_decision(
    conn: sqlite3.Connection,
    *,
    book: BookName,
    signal_id: str,
    take: bool,
    reason: str,
    detail: str,
    at: datetime,
    llm_call_id: str | None = None,
) -> bool:
    """Append a decision unless the latest one for (signal, book) already says the same."""
    latest = conn.execute(
        "SELECT take, reason_code FROM final_decisions WHERE signal_id = ? AND book = ?",
        (signal_id, book),
    ).fetchone()
    if latest is not None and latest["reason_code"] == reason and bool(latest["take"]) == take:
        return False
    conn.execute(
        "INSERT INTO decisions (signal_id, book, take, reason_code, detail, llm_call_id, "
        "decided_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (signal_id, book, int(take), reason, detail, llm_call_id, fmt_iso(at)),
    )
    return True


async def record_decision(journal: Journal, decision: BookDecision, *, at: datetime) -> bool:
    return await journal.write(
        lambda conn: insert_decision(
            conn,
            book=decision.book,
            signal_id=decision.signal_id,
            take=decision.take,
            reason=decision.reason,
            detail=decision.detail,
            at=at,
        )
    )


async def record_skips(
    journal: Journal,
    books: Iterable[BookName],
    signals: Iterable[Signal],
    reason: str,
    *,
    at: datetime,
    detail: str = "",
) -> None:
    """One decision per book for signals no book could act on (``missed_at_arm``,
    ``restart_lost``)."""
    pairs = [(book, sig) for sig in signals for book in books]

    def write(conn: sqlite3.Connection) -> None:
        for book, sig in pairs:
            insert_decision(conn, book=book, signal_id=sig.id, take=False, reason=reason,
                            detail=detail, at=at)  # fmt: skip

    await journal.write(write)


async def decided(journal: Journal, day: date) -> dict[str, frozenset[BookName]]:
    """Which books have decided each of the day's signals (for a restart)."""
    rows = await journal.fetch_all(
        "SELECT d.signal_id, d.book FROM final_decisions d JOIN signals s ON s.id = d.signal_id "
        "WHERE s.date = ?",
        (day.isoformat(),),
    )
    out: dict[str, set[BookName]] = {}
    for r in rows:
        out.setdefault(r["signal_id"], set()).add(r["book"])
    return {k: frozenset(v) for k, v in out.items()}


async def signals_with_orders(journal: Journal, day: date) -> frozenset[str]:
    rows = await journal.fetch_all(
        "SELECT DISTINCT o.signal_id FROM orders o JOIN signals s ON s.id = o.signal_id "
        "WHERE s.date = ? AND o.side = 'buy'",
        (day.isoformat(),),
    )
    return frozenset(r["signal_id"] for r in rows)


async def finalize_decisions(
    journal: Journal, book: BookName, day: date, *, at: datetime
) -> list[str]:
    """EOD: a taken entry that ended without a fill gets ``entry_unfilled`` (or
    ``broker_rejected`` if the broker refused it after accepting the submit). Running it twice
    changes nothing. Returns the signal ids it appended to."""

    def write(conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
            "SELECT d.signal_id, o.client_order_id, o.broker_order_id, o.status FROM "
            "final_decisions d JOIN signals s ON s.id = d.signal_id "
            "JOIN orders o ON o.signal_id = d.signal_id AND o.book = d.book "
            "AND o.side = 'buy' AND o.parent_client_order_id IS NULL "
            "WHERE s.date = ? AND d.book = ? AND d.take = 1 ORDER BY d.signal_id",
            (day.isoformat(), book),
        ).fetchall()
        appended: list[str] = []
        for r in rows:
            if not (is_final(r["status"]) or r["status"] == LOST):
                continue  # still open: not an outcome yet
            filled = conn.execute(
                "SELECT COALESCE(SUM(qty), 0) FROM fills WHERE broker_order_id = ?",
                (r["broker_order_id"],),
            ).fetchone()[0]
            if filled:
                continue
            reason = "broker_rejected" if r["status"] == "rejected" else "entry_unfilled"
            detail = f"entry {r['client_order_id']} {r['status']} with no fill"
            if insert_decision(conn, book=book, signal_id=r["signal_id"], take=False,
                               reason=reason, detail=detail, at=at):  # fmt: skip
                appended.append(r["signal_id"])
        return appended

    return await journal.write(write)
