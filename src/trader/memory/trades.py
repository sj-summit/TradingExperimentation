"""Trades (A§6 ``trades``; DECISIONS D-044): one row per entry that filled, from the journal.

Rebuilt at EOD with an idempotent upsert, id ``{book}-{signal_id}``:

- ``qty`` is the entry's filled quantity; entry and exit prices are VWAPs of their fills.
- ``exit_reason`` comes from the purpose of the sell that executed the most shares (ties go to
  the last); ``stop_leg`` and ``protective_stop`` both read ``stop``.
- ``pnl`` and ``r_multiple`` are gross (S§7.1: R = (exit - entry) / (entry - planned stop));
  ``fees_est`` holds the estimated sale fees.
- ``entry_slippage`` is entry - trigger; ``entry_delay_ms`` runs from the live trigger to the
  booking of the first fill, both on the PC clock: the broker stamps fills with its own clock,
  which the PC's trails by tenths of a second (D-055). A fill booked more than
  ``LIVE_BOOKING_MAX_S`` after the broker's fill time was caught up after a restart or an
  outage, so its delay is unknown: NULL.
- A position still open keeps NULL exit fields until a later rebuild sees it closed.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trader.config import BookName
from trader.core.clock import fmt_iso, parse_et_iso
from trader.memory.journal import Journal, db_to_dec, dec_to_db

# A live booking follows the broker's fill within the stream's latency or one 1 s poll, give
# or take RISK.md §9's 2 s of clock skew; later than this, the fill was caught up (D-055).
LIVE_BOOKING_MAX_S = 5.0

EXIT_REASONS: dict[str, str] = {
    "stop_leg": "stop",
    "protective_stop": "stop",
    "stop_breached": "stop_breached",
    "eod_flatten": "eod_flatten",
    "daily_loss": "daily_loss",
    "incident": "incident",
    "kill_switch": "kill_switch",
    "manual": "manual",
}


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    qty: int
    price: Decimal
    at: datetime
    purpose: str | None = None


@dataclass(frozen=True)
class TradeRow:
    id: str
    book: BookName
    signal_id: str
    symbol: str
    qty: int
    entry_ts: datetime
    entry_price: Decimal
    stop_price: Decimal | None
    exit_ts: datetime | None
    exit_price: Decimal | None
    exit_reason: str | None
    pnl: Decimal | None
    r_multiple: float | None
    fees_est: Decimal
    entry_slippage: Decimal | None
    entry_delay_ms: int | None
    eval_epoch: int | None


def build_trade(
    *,
    book: BookName,
    signal_id: str,
    symbol: str,
    stop: Decimal | None,
    trigger: Decimal | None,
    triggered_at: datetime | None,
    first_booked_at: datetime | None,
    entries: Sequence[Fill],
    exits: Sequence[Fill],
    fees: Decimal,
    epoch: int | None,
) -> TradeRow | None:
    """A trade from its fills; None when the entry never filled."""
    entries = sorted(entries, key=lambda f: f.at)
    qty = sum(f.qty for f in entries)
    if qty <= 0:
        return None
    entry = _vwap(entries)
    closing: list[Fill] = []
    left = qty
    for fill in sorted(exits, key=lambda f: f.at):
        if left <= 0:
            break
        take = min(fill.qty, left)
        closing.append(Fill(fill.fill_id, fill.order_id, take, fill.price, fill.at, fill.purpose))
        left -= take
    closed = left == 0
    exit_price = _vwap(closing) if closed else None
    first = entries[0].at
    return TradeRow(
        id=f"{book}-{signal_id}",
        book=book,
        signal_id=signal_id,
        symbol=symbol,
        qty=qty,
        entry_ts=first,
        entry_price=entry,
        stop_price=stop,
        exit_ts=closing[-1].at if closed else None,
        exit_price=exit_price,
        exit_reason=_exit_reason(closing) if closed else None,
        pnl=qty * (exit_price - entry) if exit_price is not None else None,
        r_multiple=(
            float((exit_price - entry) / (entry - stop))
            if exit_price is not None and stop is not None and entry > stop
            else None
        ),
        fees_est=fees,
        entry_slippage=entry - trigger if trigger is not None else None,
        entry_delay_ms=(
            int((first_booked_at - triggered_at).total_seconds() * 1000)
            if triggered_at
            and first_booked_at
            and (first_booked_at - first).total_seconds() <= LIVE_BOOKING_MAX_S
            else None
        ),
        eval_epoch=epoch,
    )


def _vwap(fills: Sequence[Fill]) -> Decimal:
    shares = sum(f.qty for f in fills)
    return sum((f.qty * f.price for f in fills), Decimal(0)) / shares


def _exit_reason(closing: Sequence[Fill]) -> str:
    """The purpose of the sell order that executed the most shares; ties go to the last."""
    by_order: dict[str, tuple[int, datetime, str | None]] = {}
    for f in closing:
        shares, _, _ = by_order.get(f.order_id, (0, f.at, f.purpose))
        by_order[f.order_id] = (shares + f.qty, f.at, f.purpose)
    _, _, purpose = max(by_order.values(), key=lambda v: (v[0], v[1]))
    return EXIT_REASONS.get(purpose or "", "unknown")


def read_trades(conn: sqlite3.Connection, book: BookName, day: date) -> list[TradeRow]:
    """Every trade whose entry was submitted on ``day`` (smoke tests excluded)."""
    out: list[TradeRow] = []
    entries = conn.execute(
        "SELECT o.client_order_id, o.broker_order_id, o.signal_id, o.symbol, o.stop_price, "
        "s.trigger_price, s.triggered_at, s.eval_epoch FROM orders o "
        "LEFT JOIN signals s ON s.id = o.signal_id "
        "WHERE o.book = ? AND o.side = 'buy' AND o.parent_client_order_id IS NULL "
        "AND o.submitted_at LIKE ? AND o.signal_id NOT LIKE 'smoke-%' "
        "AND o.broker_order_id IS NOT NULL ORDER BY o.submitted_at",
        (book, f"{day.isoformat()}T%"),
    ).fetchall()
    for e in entries:
        bought = _fills(
            conn,
            "SELECT f.id, f.broker_order_id, f.qty, f.price, f.filled_at, NULL AS purpose "
            "FROM fills f WHERE f.broker_order_id = ?",
            (e["broker_order_id"],),
        )
        if not bought:
            continue
        first_fill = min(bought, key=lambda f: f.at)
        first = first_fill.at
        booked = conn.execute(
            "SELECT created_at FROM ledger_entries WHERE book = ? AND ref = ?",
            (book, f"fill:{first_fill.fill_id}"),
        ).fetchone()
        sold = [
            f
            for f in _fills(
                conn,
                "SELECT f.id, f.broker_order_id, f.qty, f.price, f.filled_at, o.purpose "
                "FROM fills f JOIN orders o ON o.broker_order_id = f.broker_order_id "
                "WHERE f.book = ? AND f.symbol = ? AND f.side = 'sell' "
                "AND (o.signal_id = ? OR o.signal_id IS NULL)",
                (book, e["symbol"], e["signal_id"]),
            )
            if f.at >= first
        ]
        fees = _fee_total(conn, book, [f.fill_id for f in sold])
        trade = build_trade(
            book=book,
            signal_id=e["signal_id"],
            symbol=e["symbol"],
            stop=db_to_dec(e["stop_price"]),
            trigger=db_to_dec(e["trigger_price"]),
            triggered_at=parse_et_iso(e["triggered_at"]) if e["triggered_at"] else None,
            first_booked_at=parse_et_iso(booked[0]) if booked and booked[0] else None,
            entries=bought,
            exits=sold,
            fees=fees,
            epoch=e["eval_epoch"],
        )
        if trade is not None:
            out.append(trade)
    return out


def upsert_trades(conn: sqlite3.Connection, trades: Sequence[TradeRow]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO trades (id, book, signal_id, symbol, qty, entry_ts, entry_price, "
        "stop_price, exit_ts, exit_price, exit_reason, pnl, r_multiple, fees_est, "
        "entry_slippage, entry_delay_ms, eval_epoch) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                t.id, t.book, t.signal_id, t.symbol, t.qty, fmt_iso(t.entry_ts),
                dec_to_db(t.entry_price), dec_to_db(t.stop_price),
                fmt_iso(t.exit_ts) if t.exit_ts else None, dec_to_db(t.exit_price),
                t.exit_reason, dec_to_db(t.pnl), t.r_multiple, dec_to_db(t.fees_est),
                dec_to_db(t.entry_slippage), t.entry_delay_ms, t.eval_epoch,
            )
            for t in trades
        ],
    )  # fmt: skip


async def write_trades(journal: Journal, book: BookName, day: date) -> list[TradeRow]:
    """Rebuild the day's trades for ``book``; running it again changes nothing."""

    def write(conn: sqlite3.Connection) -> list[TradeRow]:
        trades = read_trades(conn, book, day)
        upsert_trades(conn, trades)
        return trades

    return await journal.write(write)


def _fills(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> list[Fill]:
    return [
        Fill(
            fill_id=r["id"],
            order_id=r["broker_order_id"],
            qty=int(r["qty"]),
            price=Decimal(r["price"]),
            at=parse_et_iso(r["filled_at"]),
            purpose=r["purpose"],
        )
        for r in conn.execute(sql, params)
    ]


def _fee_total(conn: sqlite3.Connection, book: BookName, fill_ids: Sequence[str]) -> Decimal:
    total = Decimal(0)
    for fill_id in fill_ids:
        for (amount,) in conn.execute(
            "SELECT amount FROM ledger_entries WHERE book = ? AND ref = ?",
            (book, f"fee-est:{fill_id}"),
        ):
            total -= Decimal(amount)
    return total
