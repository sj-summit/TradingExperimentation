"""Flattening books from outside the engine: ``trader flatten``, ``trader halt --flatten`` and the
watchdog (RISK.md §6; D-036; the Phase 6 plan's decision 14; DECISIONS D-082).

- **Each book is halted first,** with the caller's reason (``manual`` for a person, the watchdog's
  ``p1_incident`` or ``kill_switch``), so an engine still running submits nothing new.
- **The ``flattening`` marker** is renewed on every pass of ``flatten_account``, so a running
  engine leaves the book's stops alone while it works (D-036). It is cleared at the end, and it
  lapses by itself if this process dies.
- **Books are flattened concurrently:** the watchdog's run must end inside its 4-minute limit.
- **A sell an earlier flatten left ``submitting``,** its process killed between the journal and
  the broker's answer, is settled first by its client order id. Otherwise the engine's
  reconcile would skip that symbol as in flight for the rest of the day. A failed lookup is
  logged and the flatten goes on.
"""

import asyncio
from collections.abc import Mapping
from datetime import timedelta

import structlog

from trader.config import BookName, FeesConfig
from trader.core.calendar import CalendarRangeError, NotATradingDayError, TradingCalendar
from trader.core.clock import Clock, fmt_iso
from trader.execution.broker import BrokerAdapter
from trader.execution.records import SUBMITTING, UNCERTAIN, ExitPurpose, Records
from trader.execution.service import (
    FLATTEN_TIMEOUT_S,
    FlattenResult,
    MarketHours,
    flatten_account,
)
from trader.memory.journal import Journal
from trader.risk.breakers import HaltReason, Halts

log = structlog.get_logger(__name__)

MARKER_TTL = timedelta(seconds=30)
STUCK_AFTER = timedelta(seconds=30)  # a submit gets its answer in seconds; this one never did


def market_hours(calendar: TradingCalendar, clock: Clock) -> MarketHours | None:
    """Today's regular session, or None when today has none."""
    try:
        session = calendar.session(clock.now().date())
    except (CalendarRangeError, NotATradingDayError):
        return None
    return session.open, session.close


async def settle_stuck_sells(
    broker: BrokerAdapter, records: Records, journal: Journal, clock: Clock
) -> int:
    """Look up each sell of the book still ``submitting`` or ``uncertain`` well after it was
    sent: adopt what the broker has, mark the rest lost. Returns how many were settled."""
    rows = await journal.fetch_all(
        "SELECT client_order_id FROM orders WHERE book = ? AND side = 'sell' "
        "AND status IN (?, ?) AND submitted_at <= ? ORDER BY submitted_at",
        (records.book, SUBMITTING, UNCERTAIN, fmt_iso(clock.now() - STUCK_AFTER)),
    )
    for row in rows:
        cid = row["client_order_id"]
        found = await broker.order_by_client_id(cid)
        if found is None:
            await records.mark(cid, "lost")
        else:
            await records.apply(found)
        log.warning("stuck sell settled", book=records.book, order=cid, found=found is not None)
    return len(rows)


async def flatten_books(
    brokers: Mapping[BookName, BrokerAdapter],
    *,
    journal: Journal,
    calendar: TradingCalendar,
    fees: FeesConfig,
    clock: Clock,
    halt_reason: HaltReason,
    purpose: ExitPurpose,
    source: str,
    timeout_s: float = FLATTEN_TIMEOUT_S,
) -> dict[BookName, FlattenResult | None]:
    """Halt each book, then flatten them all at once; a book whose flatten raised maps to None
    (the error is logged)."""
    halts = Halts(journal, clock)
    for book in brokers:
        await halts.halt(book, halt_reason, detail=source)
    hours = market_hours(calendar, clock)

    async def one(book: BookName, broker: BrokerAdapter) -> FlattenResult | None:
        records = Records(journal, book=book, calendar=calendar, fees=fees, clock=clock)

        async def beat() -> None:  # a running engine leaves this book's stops alone meanwhile
            await halts.renew_marker(book, "flattening", MARKER_TTL)

        try:
            await settle_stuck_sells(broker, records, journal, clock)
        except Exception:  # housekeeping: never a reason not to flatten
            log.exception("stuck sells not settled", book=book, source=source)
        try:
            return await flatten_account(
                broker,
                records,
                clock=clock,
                deadline=clock.now() + timedelta(seconds=timeout_s),
                hours=hours,
                purpose=purpose,
                heartbeat=beat,
            )
        except Exception:
            log.exception("flatten failed", book=book, source=source)
            return None
        finally:
            await halts.clear(book, ["flattening"], by=source)

    done = await asyncio.gather(*(one(book, broker) for book, broker in brokers.items()))
    return dict(zip(brokers, done, strict=True))
