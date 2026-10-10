"""Settled-cash ledger (RISK.md §4): cash-account rules on a margin-enabled paper account.

Alpaca lets unsettled proceeds be spent (DECISIONS D-031), so this ledger is what keeps each
book cash-like. The rules (D-034):
- a buy uses up settled cash on its trade date;
- a sale adds its proceeds, net of the estimated fees, on Alpaca's settlement date, which skips
  bank holidays such as Columbus Day (Q8);
- deposits and adjustments count from the start of their own day;
- available = start-of-day settled cash - today's buys - the unfilled part of open entries at
  their limit (the caller passes that as ``pending``).

The rules are pure functions over ``LedgerEntry`` rows. ``SettlementLedger`` reads one book's
rows from the journal; fills are booked by execution/records.py inside the same transaction as
the fill itself. A ledger is seeded once, from a flat broker account, and refuses to answer
until then.
"""

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from trader.config import BookName, FeesConfig
from trader.core.clock import Clock, fmt_iso
from trader.memory.journal import Journal, db_to_dec, dec_to_db

Kind = Literal["deposit", "buy", "sell", "fee", "adjust"]
FEE_ESTIMATE = "fee-est:"  # ref prefix of an estimated fee; paper never charges one
SEED_REF = "seed"
CENT = Decimal("0.01")
MILLION = Decimal(1_000_000)
ZERO = Decimal(0)


class LedgerError(RuntimeError):
    """The ledger refused an operation that would break its rules."""


class LedgerNotSeededError(LedgerError):
    """The book has no opening deposit yet, so the ledger refuses to answer (fails closed)."""


class LedgerEntry(BaseModel):
    """One signed cash movement: positive adds cash, negative removes it."""

    model_config = ConfigDict(frozen=True)

    book: str
    kind: Kind
    symbol: str | None = None
    amount: Decimal
    trade_date: date
    settle_date: date
    ref: str | None = None

    @property
    def is_fee_estimate(self) -> bool:
        return self.kind == "fee" and (self.ref or "").startswith(FEE_ESTIMATE)


@dataclass(frozen=True)
class CashCheck:
    """The EOD comparison of ledger cash with broker cash (RISK.md §4)."""

    ok: bool
    ledger_cash: Decimal
    broker_cash: Decimal
    difference: Decimal  # broker - ledger


# ---------------------------------------------------------------- the rules (pure)


def settled_cash_sod(entries: Iterable[LedgerEntry], day: date) -> Decimal:
    """Settled cash at the start of ``day``: buys from earlier days, plus everything else that
    has settled by ``day``. Today's buys are subtracted by ``available``."""
    total = ZERO
    for entry in entries:
        if entry.kind == "buy":
            if entry.trade_date < day:
                total += entry.amount
        elif entry.settle_date <= day:
            total += entry.amount
    return total


def todays_buys(entries: Iterable[LedgerEntry], day: date) -> Decimal:
    """The cost of the buys filled on ``day``, as a positive amount."""
    return -sum((e.amount for e in entries if e.kind == "buy" and e.trade_date == day), ZERO)


def available(entries: Sequence[LedgerEntry], day: date, pending: Decimal = ZERO) -> Decimal:
    """Settled cash a new buy may use on ``day`` (R-009)."""
    return settled_cash_sod(entries, day) - todays_buys(entries, day) - pending


def cash(entries: Iterable[LedgerEntry], *, include_fee_estimates: bool = True) -> Decimal:
    """All cash, settled or not."""
    return sum((e.amount for e in entries if include_fee_estimates or not e.is_fee_estimate), ZERO)


def start_of_day_cash(entries: Iterable[LedgerEntry], day: date) -> Decimal:
    """Cash before ``day``'s trading: earlier trades, plus deposits and adjustments up to and
    including ``day``. With the book flat at the open, this is its start-of-day equity E."""
    total = ZERO
    for entry in entries:
        if entry.kind in ("deposit", "adjust"):
            if entry.trade_date <= day:
                total += entry.amount
        elif entry.trade_date < day:
            total += entry.amount
    return total


def estimate_sell_fees(qty: int, price: Decimal, fees: FeesConfig) -> Decimal:
    """SEC Section 31 fee plus FINRA TAF on a sale, rounded up to the cent (D-019)."""
    sec = qty * price * fees.sec_fee_per_million_sold / MILLION
    taf = min(qty * fees.finra_taf_per_share_sold, fees.finra_taf_max_per_trade)
    return (sec + taf).quantize(CENT, rounding=ROUND_CEILING)


def fill_entries(
    *,
    book: BookName,
    side: Literal["buy", "sell"],
    symbol: str,
    qty: int,
    price: Decimal,
    trade_date: date,
    settle_date: date,
    fill_id: str,
    fees: FeesConfig,
) -> list[LedgerEntry]:
    """The ledger rows for one fill: a buy's cost, or a sale's proceeds plus its fee estimate.
    Both of a sale's rows settle together, so the fee is netted against the proceeds."""

    def row(kind: Kind, amount: Decimal, ref: str) -> LedgerEntry:
        return LedgerEntry(
            book=book,
            kind=kind,
            symbol=symbol,
            amount=amount,
            trade_date=trade_date,
            settle_date=settle_date,
            ref=ref,
        )

    notional = qty * price
    if side == "buy":
        return [row("buy", -notional, f"fill:{fill_id}")]
    rows = [row("sell", notional, f"fill:{fill_id}")]
    fee = estimate_sell_fees(qty, price, fees)
    if fee:
        rows.append(row("fee", -fee, f"{FEE_ESTIMATE}{fill_id}"))
    return rows


def eod_check(
    entries: Sequence[LedgerEntry],
    broker_cash: Decimal,
    *,
    mode: Literal["paper", "live"],
    tolerance: Decimal,
) -> CashCheck:
    """Compare broker cash with the ledger. Paper charges no fees, so in paper the fee
    estimates are left out of the comparison (D-034)."""
    ledger_cash = cash(entries, include_fee_estimates=mode != "paper")
    difference = broker_cash - ledger_cash
    return CashCheck(abs(difference) <= tolerance, ledger_cash, broker_cash, difference)


# ---------------------------------------------------------------- the journal


def insert_entries(
    conn: sqlite3.Connection, entries: Sequence[LedgerEntry], created_at: datetime
) -> None:
    """Write rows inside the caller's transaction. A (book, ref) seen before raises
    IntegrityError, so a fill can never be booked twice."""
    conn.executemany(
        "INSERT INTO ledger_entries (book, kind, symbol, amount, trade_date, settle_date, ref, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                e.book,
                e.kind,
                e.symbol,
                dec_to_db(e.amount),
                e.trade_date.isoformat(),
                e.settle_date.isoformat(),
                e.ref,
                fmt_iso(created_at),
            )
            for e in entries
        ],
    )


_SELECT_BOOK = (
    "SELECT kind, symbol, amount, trade_date, settle_date, ref FROM ledger_entries "
    "WHERE book = ? ORDER BY id"
)


def read_entries(conn: sqlite3.Connection, book: str) -> list[LedgerEntry]:
    """One book's rows, read inside the caller's transaction (a consistent snapshot)."""
    return [_entry(book, row) for row in conn.execute(_SELECT_BOOK, (book,)).fetchall()]


def _entry(book: str, row: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(
        book=book,
        kind=row["kind"],
        symbol=row["symbol"],
        amount=db_to_dec(row["amount"]) or ZERO,
        trade_date=date.fromisoformat(row["trade_date"]),
        settle_date=date.fromisoformat(row["settle_date"]),
        ref=row["ref"],
    )


class SettlementLedger:
    """One book's ledger in the journal."""

    def __init__(self, journal: Journal, book: BookName, clock: Clock) -> None:
        self._journal, self.book, self._clock = journal, book, clock

    async def entries(self) -> list[LedgerEntry]:
        rows = await self._journal.fetch_all(_SELECT_BOOK, (self.book,))
        return [_entry(self.book, row) for row in rows]

    async def seeded(self) -> bool:
        row = await self._journal.fetch_one(
            "SELECT 1 FROM ledger_entries WHERE book = ? AND ref = ?", (self.book, SEED_REF)
        )
        return row is not None

    async def seed(self, amount: Decimal, *, positions: int, open_orders: int) -> LedgerEntry:
        """Open the ledger with the broker's cash. Only from a flat account with no open
        orders, and only once: otherwise the opening balance would be guesswork."""
        if positions or open_orders:
            raise LedgerError(
                f"seed only from a flat account ({positions} positions, {open_orders} orders)"
            )
        if await self.seeded():
            raise LedgerError(f"Book {self.book}'s ledger is already seeded")
        today = self._clock.now().date()
        entry = LedgerEntry(
            book=self.book,
            kind="deposit",
            amount=amount,
            trade_date=today,
            settle_date=today,
            ref=SEED_REF,
        )
        now = self._clock.now()
        await self._journal.write(lambda conn: insert_entries(conn, [entry], now))
        return entry

    async def available(self, pending: Decimal = ZERO) -> Decimal:
        return available(await self._require(), self._clock.now().date(), pending)

    async def start_of_day_equity(self) -> Decimal:
        return start_of_day_cash(await self._require(), self._clock.now().date())

    async def _require(self) -> list[LedgerEntry]:
        entries = await self.entries()
        if not any(e.ref == SEED_REF for e in entries):
            raise LedgerNotSeededError(f"Book {self.book}'s ledger has no opening deposit")
        return entries
