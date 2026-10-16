"""Settled-cash ledger (RISK.md §4): cash-account rules on a margin-enabled paper account.

Alpaca lets unsettled proceeds be spent (DECISIONS D-031), so this ledger is what keeps each
book cash-like. The rules (D-034):
- a buy uses up settled cash on its trade date;
- a sale adds its proceeds, net of the estimated fees, on Alpaca's settlement date, which skips
  bank holidays such as Columbus Day (Q8);
- the fees the broker actually charged replace the estimates once they post: Alpaca charges a
  day's fees after its close (D-055);
- deposits, adjustments and the broker's fee charges count from the start of their own day;
- available = start-of-day settled cash - today's buys - the unfilled part of open entries at
  their limit (the caller passes that as ``pending``).

The rules are pure functions over ``LedgerEntry`` rows. ``SettlementLedger`` reads one book's
rows from the journal; fills are booked by execution/records.py inside the same transaction as
the fill itself. A ledger is seeded once, from a flat broker account, and refuses to answer
until then.
"""

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from trader.config import BookName, FeesConfig
from trader.core.clock import ET, Clock, fmt_iso, parse_et_iso
from trader.execution.broker import FeeActivity
from trader.memory.journal import Journal, db_to_dec, dec_to_db

Kind = Literal["deposit", "buy", "sell", "fee", "adjust"]
FEE_ESTIMATE = "fee-est:"  # ref prefix of a sale's estimated fee
FEE_ACTUAL = "fee:"  # fee:<activity id>, a fee the broker charged (D-055)
FEE_REVERSAL = "fee-est-reversal:"  # retires a trade date's estimates once its fees are booked
FEE_VOID = "fee-void:"  # undoes a booked fee the broker later canceled
FEE_LOOKBACK = timedelta(days=10)  # every booking reads this far back, so late charges count
SESSION_CLOSE = time(16)  # a day's fees post after its close (an early close is earlier still)
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

    @property
    def is_charged_fee(self) -> bool:
        return self.kind == "fee" and (self.ref or "").startswith(FEE_ACTUAL)

    @property
    def is_broker_charge(self) -> bool:
        """A charged fee or the void of one: either posts outside the session (D-055)."""
        return self.kind == "fee" and (self.ref or "").startswith((FEE_ACTUAL, FEE_VOID))


class FeeSource(Protocol):
    """Where the fees a broker charged come from (``BrokerAdapter.fee_activities``)."""

    async def fee_activities(self, after: datetime) -> list[FeeActivity]: ...


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


def cash(entries: Iterable[LedgerEntry], *, unposted_from: date | None = None) -> Decimal:
    """All cash, settled or not. With ``unposted_from``, the fee estimates of trades from that
    date on are left out: the broker hasn't charged those fees yet (D-055)."""
    return sum(
        (
            e.amount
            for e in entries
            if unposted_from is None or not (e.is_fee_estimate and e.trade_date >= unposted_from)
        ),
        ZERO,
    )


def start_of_day_cash(entries: Iterable[LedgerEntry], day: date) -> Decimal:
    """Cash before ``day``'s trading: earlier trades, plus deposits, adjustments and the
    broker's fee charges up to and including ``day``. A charge posts overnight, and Alpaca may
    date it the next day (D-055). With the book flat at the open, this is its start-of-day
    equity E."""
    total = ZERO
    for entry in entries:
        if entry.kind in ("deposit", "adjust") or entry.is_broker_charge:
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
    entries: Sequence[LedgerEntry], broker_cash: Decimal, *, day: date, tolerance: Decimal
) -> CashCheck:
    """Compare broker cash with the ledger on ``day`` (RISK.md §4). The broker charges a day's
    fees after its close, so today's fee estimates are left out; earlier days' count, as the
    charged fees once booked and as estimates until then. Paper and live alike (D-055 amends
    D-034, which assumed paper charges no fees)."""
    ledger_cash = cash(entries, unposted_from=day)
    difference = broker_cash - ledger_cash
    return CashCheck(abs(difference) <= tolerance, ledger_cash, broker_cash, difference)


def fee_reconciliation(
    entries: Sequence[LedgerEntry],
    charged: Iterable[FeeActivity],
    *,
    book: str,
    today: date,
    seeded_at: datetime | None = None,
) -> list[LedgerEntry]:
    """The rows that book what the broker actually charged (D-055):
    - each charge not booked yet: a ``fee`` row, ref ``fee:<id>``, dated when the cash left;
    - a booked charge the broker has since canceled: a ``fee-void:<id>`` row that undoes it;
    - a trade date's fee estimates, once a charge posted after that day's close is booked:
      one ``fee-est-reversal`` row per trade and settlement date. Alpaca charges a day's fees
      after its close, whether it dates them that day or the next, so a day that sold but has
      no charge of its own counts as charged nothing. Today's estimates always stay.
    A charge from before ``seeded_at`` is ignored: the seeded cash already paid it.
    """
    refs = {e.ref for e in entries}
    booked = {(e.ref or "").removeprefix(FEE_ACTUAL): e for e in entries if e.is_charged_fee}
    kept = [f for f in charged if not _paid_by_seed(f, seeded_at)]
    rows: list[LedgerEntry] = []
    for fee in sorted(kept, key=lambda f: (f.date, f.id)):
        ref = f"{FEE_ACTUAL}{fee.id}"
        if fee.status == "canceled":
            void = f"{FEE_VOID}{fee.id}"
            if fee.id in booked and void not in refs:
                undone = booked[fee.id]
                rows.append(undone.model_copy(update={"amount": -undone.amount, "ref": void}))
                refs.add(void)
            continue
        if ref in refs:
            continue
        day = _charge_day(fee)
        rows.append(
            LedgerEntry(
                book=book,
                kind="fee",
                symbol=fee.symbol,
                amount=fee.net_amount,
                trade_date=day,
                settle_date=day,
                ref=ref,
            )
        )
        refs.add(ref)
    posted = [f for f in kept if f.status != "canceled"]
    estimates: dict[tuple[date, date], Decimal] = defaultdict(Decimal)
    for e in entries:
        if (
            e.is_fee_estimate
            and e.trade_date < today
            and any(_posted_after_close(f, e.trade_date) for f in posted)
        ):
            estimates[(e.trade_date, e.settle_date)] += e.amount
    for (trade, settle), total in sorted(estimates.items()):
        ref = _reversal_ref(trade, settle)
        if total and ref not in refs:
            rows.append(
                LedgerEntry(
                    book=book,
                    kind="fee",
                    amount=-total,
                    trade_date=trade,
                    settle_date=settle,
                    ref=ref,
                )
            )
    return rows


def fee_window_start(entries: Iterable[LedgerEntry], today: date) -> date:
    """The first day whose charges a booking reads (D-055): ``FEE_LOOKBACK`` before today, or
    the day before the oldest trade date whose estimates no charge has retired yet, whichever
    is earlier. Weeks between sessions then lose nothing; Alpaca may date a charge the day
    after its trades."""
    rows = list(entries)
    refs = {e.ref for e in rows}
    open_dates = [
        e.trade_date
        for e in rows
        if e.is_fee_estimate and _reversal_ref(e.trade_date, e.settle_date) not in refs
    ]
    start = today - FEE_LOOKBACK
    return min(start, min(open_dates) - timedelta(days=1)) if open_dates else start


def _reversal_ref(trade: date, settle: date) -> str:
    return f"{FEE_REVERSAL}{trade.isoformat()}:{settle.isoformat()}"


def _charge_day(fee: FeeActivity) -> date:
    """The day a charge's row is dated: its activity date, or the day it posted if that came
    first. The cash left then, whichever date Alpaca gives it (trade, posting or settlement
    day), so E and settled cash never count it late."""
    if fee.created_at is None:
        return fee.date
    return min(fee.date, fee.created_at.astimezone(ET).date())


def _posted_after_close(fee: FeeActivity, day: date) -> bool:
    """Whether ``fee`` posted after ``day``'s close, so ``day``'s own charge has posted too.
    Without a posting time, only a charge dated a later day counts: the estimate then stays a
    day longer, on the safe side."""
    if fee.created_at is not None:
        return fee.created_at > datetime.combine(day, SESSION_CLOSE, ET)
    return fee.date > day


def _paid_by_seed(fee: FeeActivity, seeded_at: datetime | None) -> bool:
    """A charge the seeded cash already paid. Without a posting time, only one dated before
    the seed's day counts: a double count stays on the safe side."""
    if seeded_at is None:
        return False
    if fee.created_at is not None:
        return fee.created_at <= seeded_at
    return fee.date < seeded_at.date()


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

    async def seeded_at(self) -> datetime | None:
        """When the ledger was seeded: the broker's cash then had paid every earlier charge."""
        row = await self._journal.fetch_one(
            "SELECT created_at FROM ledger_entries WHERE book = ? AND ref = ?",
            (self.book, SEED_REF),
        )
        return parse_et_iso(row["created_at"]) if row is not None and row["created_at"] else None

    async def book_posted_fees(self, source: FeeSource) -> list[LedgerEntry]:
        """Book the fees the broker charged since ``fee_window_start`` (D-055), never one the
        seeded cash already paid. The read asks from a midnight, a day before the seed's day
        at the earliest, whether Alpaca filters ``after`` by posting time or by date; the seed
        is applied here, to each charge's posting time. Reading the ledger and writing the
        rows share one transaction, so no fee is booked twice."""
        now = self._clock.now()
        seeded_at = await self.seeded_at()
        start = fee_window_start(await self._require(), now.date())
        if seeded_at is not None:
            start = max(start, seeded_at.date() - timedelta(days=1))
        charged = await source.fee_activities(datetime.combine(start, time(0), ET))

        def write(conn: sqlite3.Connection) -> list[LedgerEntry]:
            entries = read_entries(conn, self.book)
            rows = fee_reconciliation(
                entries, charged, book=self.book, today=now.date(), seeded_at=seeded_at
            )
            insert_entries(conn, rows, now)
            return rows

        return await self._journal.write(write)

    async def _require(self) -> list[LedgerEntry]:
        entries = await self.entries()
        if not any(e.ref == SEED_REF for e in entries):
            raise LedgerNotSeededError(f"Book {self.book}'s ledger has no opening deposit")
        return entries
