"""The one place that journals broker state: order rows, fills, and their ledger rows.

Every update is one journal transaction (DECISIONS D-033, D-036):
- **Orders** are upserted by ``client_order_id``. A final status is never replaced by a stale
  snapshot, and an older snapshot never replaces a newer one. Our own statuses (``submitting``,
  ``uncertain``, ``lost``) give way to any broker snapshot.
- **Fills**: whatever the source, a stream event or a REST snapshot, only the part of an
  order's cumulative ``filled_qty`` not yet journaled is booked, as one fill with id
  ``{order_id}:{cumulative qty}``, together with its ledger rows. A stream event racing a REST
  catch-up can therefore never book a fill twice.
- **Exit and stop ids** take the next attempt number from the journal, reserved by inserting the
  pending row in the same transaction.
- **Every sell says why it exists** (``orders.purpose``, DECISIONS D-044) and carries the signal
  of the entry that bought the shares, so a trade can name its exit reason.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

import structlog

from trader.config import BookName, FeesConfig
from trader.core.calendar import TradingCalendar
from trader.core.clock import Clock, fmt_iso, parse_et_iso
from trader.core.ids import Leg, client_order_id
from trader.core.models import OrderIntent, RiskCheck
from trader.execution.broker import FINAL_STATUSES, Order, Side, TradeUpdate
from trader.memory.journal import Journal, db_to_dec, dec_to_db
from trader.risk.gate import insert_risk_check
from trader.risk.ledger import fill_entries, insert_entries

log = structlog.get_logger(__name__)

# Our own statuses for orders the broker hasn't confirmed; any broker snapshot replaces them.
SUBMITTING, UNCERTAIN, LOST = "submitting", "uncertain", "lost"
OWN_STATUSES = frozenset({SUBMITTING, UNCERTAIN, LOST})

ExitPurpose = Literal[
    "stop_leg",  # an OTO entry's stop-loss leg
    "protective_stop",  # a standalone stop for shares without one (D-032), or overnight GTC
    "stop_breached",  # sold at market because the last price was already through the stop
    "eod_flatten",
    "daily_loss",
    "incident",  # a P1 or a reconcile mismatch
    "kill_switch",
    "manual",  # `trader flatten` / `trader halt --flatten`, or the smoke test
]


@dataclass(frozen=True)
class BookedFill:
    fill_id: str
    order_id: str
    client_order_id: str
    symbol: str
    side: Side
    qty: int
    price: Decimal
    at: datetime


@dataclass(frozen=True)
class OrderRow:
    """An order as the journal knows it."""

    client_order_id: str
    broker_order_id: str | None
    book: str
    signal_id: str | None
    symbol: str
    side: str
    type: str
    qty: int
    limit_price: Decimal | None
    stop_price: Decimal | None
    parent_client_order_id: str | None
    status: str
    submitted_at: datetime | None
    group_id: str | None


class Records:
    def __init__(
        self,
        journal: Journal,
        *,
        book: BookName,
        calendar: TradingCalendar,
        fees: FeesConfig,
        clock: Clock,
    ) -> None:
        self._journal, self.book = journal, book
        self._calendar, self._fees, self._clock = calendar, fees, clock

    # ---------------------------------------------------------------- before a submit

    async def pending_entry(self, intent: OrderIntent, check: RiskCheck) -> None:
        """Journal the approving check and the entry as ``submitting``, in one transaction,
        before the broker hears about it (A§10)."""
        now = self._clock.now()
        qty = check.adjusted_qty or intent.qty

        def write(conn: sqlite3.Connection) -> None:
            insert_risk_check(conn, intent, check, now)
            conn.execute(
                "INSERT INTO orders (client_order_id, book, signal_id, symbol, side, type, "
                "order_class, qty, limit_price, stop_price, status, submitted_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'buy', 'limit', 'oto', ?, ?, ?, ?, ?, ?)",
                (
                    intent.client_order_id,
                    self.book,
                    intent.signal_id,
                    intent.symbol,
                    qty,
                    dec_to_db(intent.entry_limit),
                    dec_to_db(intent.stop_price),
                    SUBMITTING,
                    fmt_iso(now),
                    fmt_iso(now),
                ),
            )

        await self._journal.write(write)

    async def pending_sell(
        self,
        *,
        symbol: str,
        qty: int,
        leg: Leg,
        purpose: ExitPurpose,
        stop_price: Decimal | None = None,
        signal_id: str | None = None,
    ) -> str:
        """Reserve the next ``exit`` or ``stop`` id for ``symbol`` today and journal the sell as
        ``submitting``; returns its client_order_id. Without a ``signal_id`` the sell takes the
        signal of the book's latest entry in the symbol: the one that bought the shares."""
        now = self._clock.now()
        prefix = client_order_id(self.book, now.date(), symbol, leg, 1).rsplit("-", 1)[0] + "-"

        def write(conn: sqlite3.Connection) -> str:
            cid = client_order_id(self.book, now.date(), symbol, leg, _next_attempt(conn, prefix))
            signal = signal_id or _entry_signal(conn, self.book, symbol)
            conn.execute(
                "INSERT INTO orders (client_order_id, book, signal_id, symbol, side, type, "
                "order_class, qty, stop_price, status, submitted_at, updated_at, purpose) "
                "VALUES (?, ?, ?, ?, 'sell', ?, 'simple', ?, ?, ?, ?, ?, ?)",
                (
                    cid,
                    self.book,
                    signal,
                    symbol,
                    "stop" if leg == "stop" else "market",
                    qty,
                    dec_to_db(stop_price),
                    SUBMITTING,
                    fmt_iso(now),
                    fmt_iso(now),
                    purpose,
                ),
            )
            return cid

        return await self._journal.write(write)

    async def mark(
        self, client_order_id: str, status: Literal["uncertain", "lost", "rejected"]
    ) -> None:
        """Settle an order the broker hasn't confirmed: still unknown, never arrived, or
        definitively refused."""
        now = self._clock.now()
        await self._journal.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE client_order_id = ? "
            "AND status IN (?, ?, ?)",
            (status, fmt_iso(now), client_order_id, SUBMITTING, UNCERTAIN, LOST),
        )

    # ---------------------------------------------------------------- broker state

    async def apply(
        self,
        order: Order,
        *,
        execution_price: Decimal | None = None,
        execution_qty: int | None = None,
        at: datetime | None = None,
        signal_id: str | None = None,
    ) -> list[BookedFill]:
        """Journal an order snapshot (with any nested legs) and book its new fills."""
        now = self._clock.now()

        def write(conn: sqlite3.Connection) -> list[BookedFill]:
            booked: list[BookedFill] = []
            for snapshot, parent in [(order, None), *((leg, order) for leg in order.legs)]:
                _upsert(conn, snapshot, self.book, parent, signal_id, now)
                booked += self._book_fills(
                    conn,
                    snapshot,
                    execution_price if snapshot is order else None,
                    execution_qty if snapshot is order else None,
                    at,
                    now,
                )
            return booked

        return await self._journal.write(write)

    async def apply_update(self, update: TradeUpdate) -> list[BookedFill]:
        return await self.apply(
            update.order, execution_price=update.price, execution_qty=update.qty, at=update.at
        )

    # ---------------------------------------------------------------- reads

    async def order(self, client_order_id: str) -> OrderRow | None:
        row = await self._journal.fetch_one(
            f"SELECT {_COLUMNS} FROM orders WHERE client_order_id = ?", (client_order_id,)
        )
        return None if row is None else _order_row(row)

    async def orders_on(self, day: date) -> list[OrderRow]:
        """The book's orders submitted on ``day``, oldest first."""
        rows = await self._journal.fetch_all(
            f"SELECT {_COLUMNS} FROM orders WHERE book = ? AND submitted_at LIKE ? "
            "ORDER BY submitted_at, client_order_id",
            (self.book, f"{day.isoformat()}T%"),
        )
        return [_order_row(row) for row in rows]

    async def planned_stop(self, symbol: str) -> Decimal | None:
        """The stop of the book's latest entry in ``symbol``: the position's plan. It is on the
        entry's own row when we journaled the intent, else on its stop leg."""
        row = await self._journal.fetch_one(
            "SELECT COALESCE(e.stop_price, (SELECT l.stop_price FROM orders l "
            "WHERE l.parent_client_order_id = e.client_order_id AND l.type = 'stop' LIMIT 1)) "
            "AS stop FROM orders e WHERE e.book = ? AND e.symbol = ? AND e.side = 'buy' "
            "AND e.parent_client_order_id IS NULL ORDER BY e.submitted_at DESC LIMIT 1",
            (self.book, symbol),
        )
        return None if row is None else db_to_dec(row["stop"])

    async def is_ours(self, order: Order) -> bool:
        """Did this book send the order (known id), or is it a leg of one (known group)?"""
        row = await self._journal.fetch_one(
            "SELECT 1 FROM orders WHERE book = ? AND (client_order_id = ? OR "
            "(group_id IS NOT NULL AND group_id = ?)) LIMIT 1",
            (self.book, order.client_order_id, order.group_id),
        )
        return row is not None

    async def first_fill_at(self, broker_order_id: str) -> datetime | None:
        row = await self._journal.fetch_one(
            "SELECT MIN(filled_at) AS first FROM fills WHERE broker_order_id = ?",
            (broker_order_id,),
        )
        return parse_et_iso(row["first"]) if row is not None and row["first"] else None

    async def filled_qty(self, broker_order_id: str) -> int:
        row = await self._journal.fetch_one(
            "SELECT COALESCE(SUM(qty), 0) AS qty FROM fills WHERE broker_order_id = ?",
            (broker_order_id,),
        )
        return 0 if row is None else int(row["qty"])

    # ---------------------------------------------------------------- internals

    def _book_fills(
        self,
        conn: sqlite3.Connection,
        order: Order,
        execution_price: Decimal | None,
        execution_qty: int | None,
        at: datetime | None,
        now: datetime,
    ) -> list[BookedFill]:
        if order.filled_qty <= 0:
            return []
        rows = conn.execute(
            "SELECT qty, price FROM fills WHERE broker_order_id = ?", (order.id,)
        ).fetchall()
        booked_qty = sum(int(r["qty"]) for r in rows)
        booked_notional = sum((int(r["qty"]) * Decimal(r["price"]) for r in rows), Decimal(0))
        delta = order.filled_qty - booked_qty
        if delta <= 0:
            return []
        if execution_price is not None and execution_qty == delta:
            price = execution_price
        elif order.filled_avg_price is not None:
            # Unrounded, so the booked fills add up to the broker's filled_qty * avg exactly.
            total = order.filled_qty * order.filled_avg_price
            price = (total - booked_notional) / delta
        else:
            log.warning("fill without a price; waiting for a snapshot", order_id=order.id)
            return []
        when = at or order.filled_at or order.updated_at or now
        fill = BookedFill(
            fill_id=f"{order.id}:{order.filled_qty}",
            order_id=order.id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            qty=delta,
            price=price,
            at=when,
        )
        trade_date = when.date()
        conn.execute(
            "INSERT INTO fills (id, broker_order_id, book, symbol, side, qty, price, filled_at, "
            "raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fill.fill_id,
                order.id,
                self.book,
                order.symbol,
                order.side,
                delta,
                dec_to_db(price),
                fmt_iso(when),
                json.dumps({"order_status": order.status, "cum_qty": order.filled_qty}),
            ),
        )
        insert_entries(
            conn,
            fill_entries(
                book=self.book,
                side=order.side,
                symbol=order.symbol,
                qty=delta,
                price=price,
                trade_date=trade_date,
                settle_date=self._calendar.settlement_date(trade_date),
                fill_id=fill.fill_id,
                fees=self._fees,
            ),
            now,
        )
        return [fill]


_COLUMNS = (
    "client_order_id, broker_order_id, book, signal_id, symbol, side, type, qty, limit_price, "
    "stop_price, parent_client_order_id, status, submitted_at, group_id"
)


# ---------------------------------------------------------------- one book's day (reads)


@dataclass(frozen=True)
class BookDay:
    """A consistent picture of one book's day, read in one transaction."""

    entries: list[tuple[OrderRow, int]]  # today's entry orders, each with its filled quantity
    held: dict[str, tuple[int, Decimal]]  # symbol -> (shares held, average cost)
    realized: Decimal  # today's realized P&L, net of the fee estimates


def read_book_day(conn: sqlite3.Connection, book: str, day: date) -> BookDay:
    """Today's entries, the shares held, and the realized P&L. The book is flat at the open, so
    today's fills explain every position (reconciliation checks that)."""
    like = f"{day.isoformat()}T%"
    filled = {
        row["broker_order_id"]: int(row["qty"])
        for row in conn.execute(
            "SELECT broker_order_id, SUM(qty) AS qty FROM fills WHERE book = ? "
            "GROUP BY broker_order_id",
            (book,),
        )
    }
    entries = [
        (row, filled.get(row.broker_order_id or "", 0))
        for row in map(
            _order_row,
            conn.execute(
                f"SELECT {_COLUMNS} FROM orders WHERE book = ? AND side = 'buy' "
                "AND parent_client_order_id IS NULL AND submitted_at LIKE ? "
                "ORDER BY submitted_at",
                (book, like),
            ),
        )
    ]
    trades: dict[str, list[Decimal]] = {}  # symbol -> [bought, cost, sold, proceeds]
    for row in conn.execute(
        "SELECT symbol, side, qty, price FROM fills WHERE book = ? AND filled_at LIKE ?",
        (book, like),
    ):
        t = trades.setdefault(row["symbol"], [Decimal(0)] * 4)
        qty, notional = Decimal(row["qty"]), Decimal(row["qty"]) * Decimal(row["price"])
        if row["side"] == "buy":
            t[0], t[1] = t[0] + qty, t[1] + notional
        else:
            t[2], t[3] = t[2] + qty, t[3] + notional
    held: dict[str, tuple[int, Decimal]] = {}
    realized = Decimal(0)
    for symbol, (bought, cost, sold, proceeds) in trades.items():
        average = cost / bought if bought else Decimal(0)
        realized += proceeds - sold * average
        if bought > sold:
            held[symbol] = (int(bought - sold), average)
    fees = conn.execute(
        "SELECT amount FROM ledger_entries WHERE book = ? AND kind = 'fee' AND trade_date = ?",
        (book, day.isoformat()),
    ).fetchall()
    realized += sum((Decimal(r["amount"]) for r in fees), Decimal(0))
    return BookDay(entries=entries, held=held, realized=realized)


def _upsert(
    conn: sqlite3.Connection,
    order: Order,
    book: str,
    parent: Order | None,
    signal_id: str | None,
    now: datetime,
) -> None:
    row = conn.execute(
        "SELECT status, updated_at, signal_id, parent_client_order_id FROM orders "
        "WHERE client_order_id = ?",
        (order.client_order_id,),
    ).fetchone()
    parent_cid = parent.client_order_id if parent is not None else None
    if parent_cid is None and order.group_role not in (None, "entry") and order.group_id:
        entry = conn.execute(  # a leg from the stream: find its entry through the group id
            "SELECT client_order_id, signal_id FROM orders WHERE group_id = ? AND book = ? "
            "AND parent_client_order_id IS NULL AND client_order_id != ?",
            (order.group_id, book, order.client_order_id),
        ).fetchone()
        if entry is not None:
            parent_cid, signal_id = entry["client_order_id"], signal_id or entry["signal_id"]
    if parent_cid is not None and signal_id is None:
        found = conn.execute(
            "SELECT signal_id FROM orders WHERE client_order_id = ?", (parent_cid,)
        ).fetchone()
        signal_id = None if found is None else found["signal_id"]
    raw = json.dumps(order.raw, sort_keys=True, default=str)
    updated = fmt_iso(order.updated_at or now)
    purpose = "stop_leg" if parent_cid is not None and order.side == "sell" else None
    if row is None:
        conn.execute(
            "INSERT INTO orders (client_order_id, broker_order_id, book, signal_id, symbol, "
            "side, type, order_class, qty, limit_price, stop_price, parent_client_order_id, "
            "status, submitted_at, updated_at, raw_json, group_id, purpose) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                order.client_order_id,
                order.id,
                book,
                signal_id,
                order.symbol,
                order.side,
                order.type,
                order.order_class,
                order.qty,
                dec_to_db(order.limit_price),
                dec_to_db(order.stop_price),
                parent_cid,
                order.status,
                fmt_iso(order.submitted_at or now),
                updated,
                raw,
                order.group_id,
                purpose,
            ),
        )
        return
    stored_status, stored_updated = row["status"], row["updated_at"]
    status, updated_at = order.status, updated
    if stored_status not in OWN_STATUSES:
        stale = (
            order.updated_at is not None
            and stored_updated is not None
            and order.updated_at < parse_et_iso(stored_updated)
        )
        if (stored_status in FINAL_STATUSES and order.status not in FINAL_STATUSES) or stale:
            status, updated_at = stored_status, stored_updated
    conn.execute(
        "UPDATE orders SET broker_order_id = ?, status = ?, updated_at = ?, raw_json = ?, "
        "group_id = COALESCE(group_id, ?), "
        "signal_id = COALESCE(signal_id, ?), "
        "parent_client_order_id = COALESCE(parent_client_order_id, ?), "
        "purpose = COALESCE(purpose, ?) "
        "WHERE client_order_id = ?",
        (
            order.id,
            status,
            updated_at,
            raw,
            order.group_id,
            signal_id,
            parent_cid,
            purpose,
            order.client_order_id,
        ),
    )


def _entry_signal(conn: sqlite3.Connection, book: str, symbol: str) -> str | None:
    row = conn.execute(
        "SELECT signal_id FROM orders WHERE book = ? AND symbol = ? AND side = 'buy' "
        "AND parent_client_order_id IS NULL AND signal_id IS NOT NULL "
        "ORDER BY submitted_at DESC LIMIT 1",
        (book, symbol),
    ).fetchone()
    return None if row is None else str(row["signal_id"])


def _next_attempt(conn: sqlite3.Connection, prefix: str) -> int:
    rows = conn.execute(
        "SELECT client_order_id FROM orders WHERE client_order_id LIKE ?", (f"{prefix}%",)
    ).fetchall()
    attempts = [int(tail) for (cid,) in rows if (tail := cid[len(prefix) :]).isdigit()]
    return max(attempts, default=0) + 1


def _order_row(row: sqlite3.Row) -> OrderRow:
    return OrderRow(
        client_order_id=row["client_order_id"],
        broker_order_id=row["broker_order_id"],
        book=row["book"],
        signal_id=row["signal_id"],
        symbol=row["symbol"],
        side=row["side"],
        type=row["type"],
        qty=int(row["qty"] or 0),
        limit_price=db_to_dec(row["limit_price"]),
        stop_price=db_to_dec(row["stop_price"]),
        parent_client_order_id=row["parent_client_order_id"],
        status=row["status"],
        submitted_at=parse_et_iso(row["submitted_at"]) if row["submitted_at"] else None,
        group_id=row["group_id"],
    )
