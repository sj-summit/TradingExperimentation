"""End of day (A§3 16:05; RISK.md §4-5; DECISIONS D-048): bookkeeping and breakers, once a day.

Per book, in order:
1. **reconcile:** catch up on the day's orders over REST and compare with the journal;
2. **decisions:** ``finalize_decisions`` (``entry_unfilled``/``broker_rejected``), and
   ``restart_lost`` for a trigger the book never decided;
3. **trades:** rebuild the day's trades;
4. **fees:** book the fees the broker charged lately (D-055), when the day's capabilities say so;
5. **stats:** the ``daily_stats`` row;
6. **breakers:** the cash check (its residual is stored), the drawdown breaker and the losing
   streak.

Each step is marked done in ``day_runs`` and a restart skips it. The first four are idempotent
anyway; the markers matter for the breakers, which must count a day once.
"""

import json
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal

import structlog

from trader.books.base import finalize_decisions, record_skips
from trader.config import BookName, RiskConfig
from trader.core.clock import ET, Clock, fmt_iso
from trader.core.models import FeatureValue
from trader.execution.broker import BrokerAdapter, BrokerError
from trader.execution.reconcile import Reconciler
from trader.execution.records import Records
from trader.execution.service import PriceSource
from trader.memory.incidents import Severity, record_incident
from trader.memory.journal import Journal, db_to_dec, dec_to_db
from trader.memory.trades import write_trades
from trader.orchestrator.capabilities import FEES
from trader.risk.breakers import (
    BookStats,
    HaltReason,
    Halts,
    drawdown_breached,
    drawdown_pct,
    losing_streak_breached,
)
from trader.risk.ledger import (
    SettlementLedger,
    cash,
    eod_check,
    read_entries,
    start_of_day_cash,
)
from trader.strategy.orb_sip import load_signals

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------- day_runs


async def record_state(journal: Journal, day: date, state: str, at: datetime) -> None:
    """A state transition: logged in ``day_runs`` and kept in ``trading_days.run_status``."""

    def write(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO day_runs (date, kind, name, at) VALUES (?, 'state', ?, ?)",
            (day.isoformat(), state, fmt_iso(at)),
        )
        conn.execute(
            "UPDATE trading_days SET run_status = ? WHERE date = ?", (state, day.isoformat())
        )

    await journal.write(write)


async def run_status(journal: Journal, day: date) -> str | None:
    row = await journal.fetch_one(
        "SELECT run_status FROM trading_days WHERE date = ?", (day.isoformat(),)
    )
    return None if row is None else row["run_status"]


async def step_done(journal: Journal, day: date, name: str) -> bool:
    row = await journal.fetch_one(
        "SELECT 1 FROM day_runs WHERE date = ? AND kind = 'step' AND name = ?",
        (day.isoformat(), name),
    )
    return row is not None


async def mark_step(journal: Journal, day: date, name: str, at: datetime, detail: str = "") -> None:
    await journal.execute(
        "INSERT OR IGNORE INTO day_runs (date, kind, name, at, detail) VALUES (?, 'step', ?, ?, ?)",
        (day.isoformat(), name, fmt_iso(at), detail or None),
    )


async def once(
    journal: Journal, clock: Clock, day: date, name: str, job: Callable[[], Awaitable[object]]
) -> bool:
    """Run ``job`` unless the step is already marked done; mark it when it finishes."""
    if await step_done(journal, day, name):
        return False
    await job()
    await mark_step(journal, day, name, clock.now())
    return True


# ---------------------------------------------------------------- one book's EOD


@dataclass(frozen=True)
class BookClose:
    """What one book's EOD needs."""

    book: BookName
    broker: BrokerAdapter
    records: Records
    max_drawdown: Decimal | None  # marked, in $, from the heartbeat; None if none sampled


@dataclass(frozen=True)
class EodContext:
    journal: Journal
    clock: Clock
    day: date
    risk: RiskConfig
    halts: Halts
    stats: BookStats
    prices: PriceSource
    regime: Mapping[str, FeatureValue]
    capabilities: frozenset[str] = frozenset()  # what the day's engine does (capabilities.py)


async def close_book(ctx: EodContext, b: BookClose) -> None:
    """Run the book's EOD steps, skipping those already done today."""
    name = b.book
    await once(ctx.journal, ctx.clock, ctx.day, f"reconcile:{name}", lambda: _reconcile(ctx, b))
    await once(ctx.journal, ctx.clock, ctx.day, f"decisions:{name}", lambda: _decisions(ctx, b))
    await once(
        ctx.journal,
        ctx.clock,
        ctx.day,
        f"trades:{name}",
        lambda: write_trades(ctx.journal, name, ctx.day),
    )
    if FEES in ctx.capabilities:
        await once(ctx.journal, ctx.clock, ctx.day, f"fees:{name}", lambda: _fees(ctx, b))
    await once(ctx.journal, ctx.clock, ctx.day, f"stats:{name}", lambda: _stats(ctx, b))
    await once(ctx.journal, ctx.clock, ctx.day, f"breakers:{name}", lambda: _breakers(ctx, b))


async def _reconcile(ctx: EodContext, b: BookClose) -> None:
    reconciler = Reconciler(book=b.book, broker=b.broker, records=b.records, journal=ctx.journal)
    found = await reconciler.check(since=datetime.combine(ctx.day, time(0), ET))
    if found.empty:
        return
    detail: dict[str, object] = {
        "unknown_positions": found.unknown_positions,
        "qty_mismatches": {s: list(q) for s, q in found.qty_mismatches.items()},
        "unknown_orders": list(found.unknown_orders),
    }
    await _incident(ctx, "P2", "eod_reconcile_mismatch", f"Book {b.book}: {detail}", detail)


async def _decisions(ctx: EodContext, b: BookClose) -> None:
    now = ctx.clock.now()
    await finalize_decisions(ctx.journal, b.book, ctx.day, at=now)
    rows = await ctx.journal.fetch_all(
        "SELECT s.id FROM signals s WHERE s.date = ? AND s.status = 'triggered' AND NOT EXISTS "
        "(SELECT 1 FROM final_decisions d WHERE d.signal_id = s.id AND d.book = ?) ORDER BY s.id",
        (ctx.day.isoformat(), b.book),
    )
    lost = {r["id"] for r in rows}
    if lost:
        signals = [s for s in await load_signals(ctx.journal, ctx.day) if s.id in lost]
        await record_skips(
            ctx.journal, [b.book], signals, "restart_lost", at=now, detail="no decision by EOD"
        )


async def _fees(ctx: EodContext, b: BookClose) -> None:
    """Book what the broker charged lately (D-055), so the cash check compares like with like.
    Fees that can't be read now are booked by a later boot or EOD."""
    try:
        rows = await SettlementLedger(ctx.journal, b.book, ctx.clock).book_posted_fees(b.broker)
    except BrokerError as exc:
        await _incident(ctx, "P3", "fees_unavailable", f"Book {b.book}: fees not read: {exc}")
        return
    if rows:
        net = sum((r.amount for r in rows), Decimal(0))
        log.info("fees booked", book=b.book, rows=len(rows), net=str(net))


async def _stats(ctx: EodContext, b: BookClose) -> None:
    """The day's row. Its start is the ledger's start of day after the fees step, not boot's
    E: a charge booked since boot paid earlier trades, so it stays out of today's pnl (D-055)."""
    entries = await ctx.journal.read(lambda conn: read_entries(conn, b.book))
    marked = Decimal(0)
    for position in await b.broker.positions():
        if position.qty:
            last = ctx.prices.last_price(position.symbol) or position.current_price
            marked += position.qty * (last or position.avg_entry_price)
    start_equity = start_of_day_cash(entries, ctx.day)
    end_equity = cash(entries) + marked
    day = ctx.day.isoformat()

    def write(conn: sqlite3.Connection) -> None:
        count = conn.execute("SELECT COUNT(*) FROM signals WHERE date = ?", (day,)).fetchone()[0]
        triggered = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE date = ? AND status = 'triggered'", (day,)
        ).fetchone()[0]
        trades = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE book = ? AND entry_ts LIKE ?", (b.book, f"{day}T%")
        ).fetchone()[0]
        epoch = conn.execute("SELECT value FROM meta WHERE key = 'eval_epoch'").fetchone()
        conn.execute(
            "INSERT OR REPLACE INTO daily_stats (date, book, start_equity, end_equity, pnl, "
            "n_signals, n_triggered, n_trades, max_intraday_dd, regime_json, eval_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                day,
                b.book,
                dec_to_db(start_equity),
                dec_to_db(end_equity),
                dec_to_db(end_equity - start_equity),
                count,
                triggered,
                trades,
                None if b.max_drawdown is None else dec_to_db(b.max_drawdown),
                json.dumps(dict(ctx.regime), sort_keys=True),
                int(epoch[0]) if epoch is not None else 0,
            ),
        )

    await ctx.journal.write(write)


async def _breakers(ctx: EodContext, b: BookClose) -> None:
    """RISK.md §4-5: a cash mismatch over the tolerance, a 10% drawdown from the peak, or five
    losing sessions in a row each halt the book until a human resumes it, with a P2."""
    row = await ctx.journal.fetch_one(
        "SELECT end_equity, pnl, n_trades FROM daily_stats WHERE date = ? AND book = ?",
        (ctx.day.isoformat(), b.book),
    )
    if row is None:
        raise RuntimeError(f"no daily_stats row for Book {b.book} on {ctx.day}")
    end_equity = db_to_dec(row["end_equity"]) or Decimal(0)
    pnl = db_to_dec(row["pnl"]) or Decimal(0)
    entries = await ctx.journal.read(lambda conn: read_entries(conn, b.book))
    broker_cash = (await b.broker.account()).cash
    tolerance = ctx.risk.reconcile_cash_tolerance
    checked = eod_check(entries, broker_cash, day=ctx.day, tolerance=tolerance)
    await ctx.journal.execute(
        "UPDATE daily_stats SET cash_residual = ? WHERE date = ? AND book = ?",
        (dec_to_db(checked.difference), ctx.day.isoformat(), b.book),
    )
    compared = (
        f"Book {b.book}: broker cash {checked.broker_cash} vs ledger {checked.ledger_cash} "
        f"(difference {checked.difference})"
    )
    if not checked.ok:
        await _halt(ctx, b.book, "cash_mismatch", compared)
    elif abs(checked.difference) > tolerance / 2:
        await _incident(ctx, "P3", "cash_drift", f"{compared}: over half the tolerance {tolerance}")
    peak = await ctx.stats.raise_peak(b.book, end_equity)
    if drawdown_breached(peak, end_equity, ctx.risk.drawdown_breaker_pct):
        pct = drawdown_pct(peak, end_equity).quantize(Decimal("0.01"))
        await _halt(ctx, b.book, "drawdown", f"Book {b.book}: {pct}% below its peak {peak}")
    streak = await ctx.stats.record_day(b.book, ctx.day, pnl, traded=row["n_trades"] > 0)
    if losing_streak_breached(streak, ctx.risk.consecutive_losing_days_breaker):
        await _halt(ctx, b.book, "losing_streak", f"Book {b.book}: {streak} losing sessions")
    log.info(
        "eod breakers",
        book=b.book,
        cash_ok=checked.ok,
        broker_cash=str(checked.broker_cash),
        ledger_cash=str(checked.ledger_cash),
        residual=str(checked.difference),
        peak=str(peak),
        streak=streak,
    )


async def _halt(ctx: EodContext, book: BookName, reason: HaltReason, message: str) -> None:
    await ctx.halts.halt(book, reason, detail=message)
    await _incident(ctx, "P2", reason, f"{message}; halted until `trader resume`")


async def _incident(
    ctx: EodContext,
    severity: Severity,
    code: str,
    message: str,
    detail: dict[str, object] | None = None,
) -> None:
    await record_incident(
        ctx.journal,
        at=ctx.clock.now(),
        severity=severity,
        component="eod",
        code=code,
        message=message,
        detail=detail,
    )
