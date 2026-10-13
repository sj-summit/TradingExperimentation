"""Execution (A§4.6): the only module that sends orders to the broker (CLAUDE.md invariant 1).

- ``flatten_account`` is the one flatten routine (DECISIONS D-036). The kill switch,
  ``trader flatten``, ``trader halt --flatten``, the engine, and later the watchdog all use it.
  It works over REST alone, so it runs without the engine.
- ``ExecutionService`` is one book's executor (D-032, D-033, D-035, D-037). It submits approved
  entries under the book's lock, tracks them through the trade stream and REST polling,
  protects every position, flattens, and reconciles.
"""

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol

import structlog

from trader.config import BookName, RiskConfig
from trader.core.calendar import DayTimes
from trader.core.clock import Clock, parse_et_iso
from trader.core.models import OrderIntent, RiskCheck
from trader.execution.broker import (
    WORKING_STOP_STATUSES,
    BrokerAdapter,
    BrokerAuthError,
    BrokerError,
    DuplicateClientOrderId,
    Order,
    OrderNotCancelable,
    OrderNotFound,
    OrderRejected,
    Position,
    StreamResync,
    SubmitUncertain,
    is_final,
    stop_is_working,
)
from trader.execution.reconcile import ReconcileDiff, Reconciler
from trader.execution.records import (
    OWN_STATUSES,
    BookDay,
    BookedFill,
    ExitPurpose,
    Records,
    read_book_day,
)
from trader.memory.incidents import Severity, record_incident
from trader.memory.journal import Journal
from trader.risk.breakers import Halts, daily_loss_breached
from trader.risk.gate import GateState, RiskGate, record_risk_check
from trader.risk.ledger import (
    SEED_REF,
    LedgerEntry,
    LedgerNotSeededError,
    available,
    cash,
    read_entries,
    start_of_day_cash,
)

log = structlog.get_logger(__name__)

FLATTEN_POLL_S = 0.5
LOOKUP_RETRY_S = 1.0
CANCEL_SETTLE_POLLS = 20  # after the close: wait up to 20 polls for cancels to become final

MarketHours = tuple[datetime, datetime]  # today's regular session: (open, close)


@dataclass(frozen=True)
class FlattenResult:
    flat: bool
    remaining: dict[str, int] = field(default_factory=dict)  # symbol -> shares still held
    market_closed: bool = False  # nothing could be sold: positions keep GTC stops instead
    protected: dict[str, Decimal] = field(default_factory=dict)  # symbol -> its GTC stop price
    unprotected: dict[str, int] = field(default_factory=dict)  # symbol -> shares without one


async def flatten_account(
    broker: BrokerAdapter,
    records: Records,
    *,
    clock: Clock,
    deadline: datetime,
    hours: MarketHours | None,
    purpose: ExitPurpose,
    heartbeat: Callable[[], Awaitable[object]] | None = None,
    poll_s: float = FLATTEN_POLL_S,
) -> FlattenResult:
    """Make the account flat: cancel every order, wait until each is final, re-read the
    positions, sell the shares no order holds, and repeat until flat or ``deadline``.

    The caller has already halted the book, so nothing new is submitted meanwhile. Every sell
    it places carries ``purpose`` (D-044).

    Outside ``hours`` nothing can be sold (a sell would only queue for the next open), and
    cancelling the stops would leave positions bare. So instead every remaining position gets a
    GTC stop at its planned stop, since day orders expire at the close, and the caller raises a
    P1. The next boot flattens at 09:30:05 (A§3).
    """
    poll = timedelta(seconds=poll_s)
    watched: set[str] = set()  # every order touched, journaled until final (no stream here)
    while True:
        if heartbeat is not None:
            await heartbeat()
        await _refresh(broker, records, watched)
        if not _open(hours, clock.now()):
            return await _protect_overnight(broker, records, clock, poll)
        open_orders = await broker.open_orders()
        for order in open_orders:
            await records.apply(order)
        for order in _with_legs(open_orders):
            watched.add(order.id)
            await _cancel_quietly(broker, order)
        if open_orders:
            await clock.sleep_until(clock.now() + poll)
            if await broker.open_orders():
                if clock.now() >= deadline:
                    await _refresh(broker, records, watched)
                    return FlattenResult(flat=False, remaining=await _held(broker))
                continue
        held = await _held_positions(broker)
        if not held:
            await _refresh(broker, records, watched)
            return FlattenResult(flat=True)
        for position in held:
            if position.qty > 0 and position.qty_available > 0:  # never sell into a short
                sold = await _sell(broker, records, clock, position, purpose)
                if sold is not None:
                    watched.add(sold.id)
        if clock.now() >= deadline:
            await _refresh(broker, records, watched)
            return FlattenResult(flat=False, remaining={p.symbol: p.qty for p in held})
        await clock.sleep_until(clock.now() + poll)


async def _sell(
    broker: BrokerAdapter,
    records: Records,
    clock: Clock,
    position: Position,
    purpose: ExitPurpose,
) -> Order | None:
    """Market-sell the shares no order holds. Resending after an uncertain submit is safe for a
    sell: the account can't short, so a second sell of the same shares is rejected (D-033)."""
    cid = await records.pending_sell(
        symbol=position.symbol, qty=position.qty_available, leg="exit", purpose=purpose
    )
    try:
        order = await broker.submit_market_sell(position.symbol, position.qty_available, cid)
    except DuplicateClientOrderId:
        found = await broker.order_by_client_id(cid)
        if found is None:
            return None
        order = found
    except SubmitUncertain:
        looked_up = await _look_up(broker, clock, cid)
        if looked_up is None:
            await records.mark(cid, "lost")
            return None
        order = looked_up
    except OrderRejected as exc:
        await records.mark(cid, "rejected")
        log.warning("flatten sell rejected", symbol=position.symbol, reason=exc.reason)
        return None
    await records.apply(order)
    return order


async def _protect_overnight(
    broker: BrokerAdapter, records: Records, clock: Clock, poll: timedelta
) -> FlattenResult:
    """Outside market hours: keep working GTC stops, cancel anything else (such as a sell
    queued for the open), and give every uncovered position a GTC stop at its planned stop."""
    open_orders = await broker.open_orders()
    covered: dict[str, int] = {}
    protected: dict[str, Decimal] = {}
    cancelled = False
    for order in _with_legs(open_orders):
        await records.apply(order)
        if _overnight_stop(order):
            covered[order.symbol] = covered.get(order.symbol, 0) + order.qty - order.filled_qty
            if order.stop_price is not None:
                protected[order.symbol] = order.stop_price
        elif not order.final:
            await _cancel_quietly(broker, order)
            cancelled = True
    # A cancel's answer only means requested: shares stay held (qty_available 0) until it is
    # final, and a stop sized from them would come up short (DECISIONS D-051).
    for _ in range(CANCEL_SETTLE_POLLS if cancelled else 0):
        await clock.sleep_until(clock.now() + poll)
        still = [o for o in _with_legs(await broker.open_orders()) if not o.final]
        if all(_overnight_stop(o) for o in still):
            break
    held = await _held_positions(broker)
    unprotected: dict[str, int] = {}
    for position in held:
        uncovered = position.qty - covered.get(position.symbol, 0)
        need = min(uncovered, position.qty_available)
        stop = await records.planned_stop(position.symbol)
        if need > 0 and stop is not None:
            cid = await records.pending_sell(
                symbol=position.symbol,
                qty=need,
                leg="stop",
                purpose="protective_stop",
                stop_price=stop,
            )
            try:
                order = await broker.submit_stop(position.symbol, need, stop, cid, tif="gtc")
            except BrokerError as exc:
                await records.mark(cid, "lost" if isinstance(exc, SubmitUncertain) else "rejected")
                log.error("overnight stop failed", symbol=position.symbol, error=str(exc))
            else:
                await records.apply(order)
                protected[position.symbol] = stop
                uncovered -= need
        if uncovered > 0 and position.qty > 0:
            unprotected[position.symbol] = uncovered
            log.error(
                "shares left without a GTC stop overnight",
                symbol=position.symbol,
                shares=uncovered,
                planned_stop=str(stop),
            )
    return FlattenResult(
        flat=not held,
        remaining={p.symbol: p.qty for p in held},
        market_closed=True,
        protected={s: p for s, p in protected.items() if any(h.symbol == s for h in held)},
        unprotected=unprotected,
    )


async def _look_up(broker: BrokerAdapter, clock: Clock, cid: str) -> Order | None:
    """An uncertain submit: look the id up now and once more a second later."""
    found = await broker.order_by_client_id(cid)
    if found is None:
        await clock.sleep_until(clock.now() + timedelta(seconds=LOOKUP_RETRY_S))
        found = await broker.order_by_client_id(cid)
    return found


async def _refresh(broker: BrokerAdapter, records: Records, watched: set[str]) -> None:
    """Journal the latest state of the orders we touched, and their fills, until each is final.
    A stop that fills while its cancel is pending is booked this way."""
    for order_id in sorted(watched):
        try:
            order = await broker.get_order(order_id)
        except OrderNotFound:
            watched.discard(order_id)
            continue
        await records.apply(order)
        if order.final:
            watched.discard(order_id)


async def _cancel_quietly(broker: BrokerAdapter, order: Order) -> None:
    if order.final or order.status == "pending_cancel":
        return
    with contextlib.suppress(OrderNotCancelable, OrderNotFound):  # final, or gone with its group
        await broker.cancel(order.id)


def _open(hours: MarketHours | None, now: datetime) -> bool:
    return hours is not None and hours[0] <= now < hours[1]


def _with_legs(orders: list[Order]) -> list[Order]:
    return [o for order in orders for o in (order, *order.legs)]


def _overnight_stop(order: Order) -> bool:
    """A working GTC sell stop: what keeps a position protected after the close."""
    return order.side == "sell" and order.time_in_force == "gtc" and stop_is_working(order)


async def _held_positions(broker: BrokerAdapter) -> list[Position]:
    return [p for p in await broker.positions() if p.qty != 0]


async def _held(broker: BrokerAdapter) -> dict[str, int]:
    return {p.symbol: p.qty for p in await _held_positions(broker)}


# ---------------------------------------------------------------- the book's executor

PARTIAL_FILL_GRACE_S = 1.0  # a partly filled entry gets this long to finish (D-032)
ENTRY_POLL_S = 1.0  # open entries are polled over REST, whatever the stream says
PROTECTION_REFRESH_S = 5.0  # positions and open orders are re-read at least this often (A§3)
RECONCILE_S = 60.0
RECONCILE_CONFIRM_S = 5.0  # a difference must show on a second pass this much later (D-037)
FLATTEN_TIMEOUT_S = 60.0
SUBMIT_DRAIN_S = 15.0  # how long a flatten waits for submits already in flight
DRAIN_STEP = timedelta(milliseconds=100)


class PriceSource(Protocol):
    """The latest trade price per symbol (the market-data hub)."""

    def last_price(self, symbol: str) -> Decimal | None: ...


@dataclass(frozen=True)
class TradingDay:
    """The session a service trades, and when its session-scoped halts end."""

    times: DayTimes
    next_open: datetime

    @property
    def hours(self) -> MarketHours:
        return (self.times.open, self.times.close)


@dataclass(frozen=True)
class Exposure:
    """What the service knows that the gate needs (D-035); the caller adds the rest."""

    symbols: frozenset[str]  # held, or with an open or uncertain entry
    entries_today: int
    symbols_entered_today: frozenset[str]
    available_cash: Decimal
    equity_sod: Decimal
    realized_pnl_today: Decimal
    open_risk: Decimal
    ledger_equity: Decimal
    unprotected: frozenset[str]
    client_order_id_used: bool
    open_order_for_signal: bool


EntryOutcome = Literal["submitted", "rejected", "broker_rejected", "uncertain", "halted"]


@dataclass(frozen=True)
class EntryResult:
    outcome: EntryOutcome
    check: RiskCheck | None = None
    order: Order | None = None
    reason: str = ""


GateStateBuilder = Callable[[Exposure], Awaitable[GateState]]


@dataclass
class _Entry:
    """An entry this service submitted or adopted, tracked until it is final."""

    client_order_id: str
    signal_id: str
    symbol: str
    qty: int
    submitted_at: datetime
    status: str = "submitting"
    order_id: str | None = None
    filled_qty: int = 0
    first_fill_at: datetime | None = None
    cancel_requested: bool = False
    give_up_at: datetime | None = None  # an uncertain entry is called lost after this

    @property
    def open(self) -> bool:
        return self.status in ("submitting", "uncertain") or not (
            is_final(self.status) or self.status in OWN_STATUSES
        )


@dataclass(frozen=True)
class _View:
    """The broker's positions and open orders, read together."""

    positions: dict[str, Position]
    open_orders: list[Order]


def _snapshot(conn: sqlite3.Connection, book: str, day: date) -> tuple[BookDay, list[LedgerEntry]]:
    return read_book_day(conn, book, day), read_entries(conn, book)


class ExecutionService:
    """One book's executor. Every timer runs on the injected clock."""

    def __init__(
        self,
        *,
        book: BookName,
        broker: BrokerAdapter,
        journal: Journal,
        records: Records,
        halts: Halts,
        gate: RiskGate,
        prices: PriceSource,
        clock: Clock,
        risk: RiskConfig,
        day: TradingDay,
        both_books: tuple[BookName, ...] = ("A", "B"),
    ) -> None:
        self.book = book
        self._broker, self._journal, self._records = broker, journal, records
        self._halts, self._gate, self._prices, self._clock = halts, gate, prices, clock
        self._risk, self._day, self._both_books = risk, day, both_books
        self._reconciler = Reconciler(book=book, broker=broker, records=records, journal=journal)
        self._lock = asyncio.Lock()
        self._flatten_lock = asyncio.Lock()  # one flatten at a time (D-044)
        self._entries: dict[str, _Entry] = {}
        self._view: _View | None = None
        self._bare: dict[str, datetime] = {}  # symbol -> when its unprotected time started
        self._covered_once: set[str] = set()
        self._stop_pending: set[str] = set()
        self._partial: set[str] = set()  # protected after a partial fill: a P3 note
        self._p1_symbols: set[str] = set()
        self._closing = False  # no new entries for the rest of the day
        self._flattening = False
        self._flattens = 0
        self._daily_loss_hit = False
        self._in_flight = 0
        self._wake = asyncio.Event()
        self._resync = True
        self._last_refresh: datetime | None = None
        self._next_reconcile = clock.now()
        self._confirm_at: datetime | None = None
        self._reported: str | None = None  # the mismatch already reported, until it clears
        self._tasks: list[asyncio.Task[None]] = []
        self.equity_sod = Decimal(0)

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        await self.recover()
        self._tasks = [
            asyncio.create_task(self._consume(), name=f"trades-{self.book}"),
            asyncio.create_task(self._monitor(), name=f"execution-{self.book}"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def recover(self) -> None:
        """Adopt the broker's state at boot or after a crash (A§3): entries we journaled but
        don't track, and whatever the stream never delivered (the first tick reconciles)."""
        today = self._clock.now().date()
        _, ledger = await self._journal.read(lambda conn: _snapshot(conn, self.book, today))
        if not any(e.ref == SEED_REF for e in ledger):
            raise LedgerNotSeededError(f"Book {self.book}'s ledger has no opening deposit")
        self.equity_sod = start_of_day_cash(ledger, today)
        for row in await self._records.orders_on(today):
            if row.side != "buy" or row.parent_client_order_id is not None:
                continue
            unconfirmed = row.status in ("submitting", "uncertain")
            if not unconfirmed and (is_final(row.status) or row.status in OWN_STATUSES):
                continue
            entry = _Entry(
                client_order_id=row.client_order_id,
                signal_id=row.signal_id or "",
                symbol=row.symbol,
                qty=row.qty,
                submitted_at=row.submitted_at or self._clock.now(),
                status=row.status,
                order_id=row.broker_order_id,
            )
            self._entries[entry.client_order_id] = entry
            found = await self._broker.order_by_client_id(entry.client_order_id)
            if found is not None:
                await self._adopt(entry, found)
            elif unconfirmed:
                entry.status = "uncertain"
                entry.give_up_at = self._give_up_time(entry.submitted_at)
        self._resync = True

    # ---------------------------------------------------------------- entries

    async def try_enter(self, intent: OrderIntent, build_state: GateStateBuilder) -> EntryResult:
        """Check an entry against the gate and submit it if approved.

        The gate state is built, checked, and the pending order journaled under the book's
        lock, so two signals can't both pass on the same capacity (D-035). The submit happens
        after the lock: the pending order already counts in the exposure.
        """
        async with self._lock:
            if self._closing or self._flattening:
                return EntryResult("halted", reason="the book is flattening or closed")
            state = await build_state(await self.exposure(intent))
            check = self._gate.check(intent, state)
            if not check.approved:
                await record_risk_check(self._journal, intent, check, self._clock.now())
                return EntryResult("rejected", check)
            sized = intent.model_copy(update={"qty": check.adjusted_qty or intent.qty})
            await self._records.pending_entry(sized, check)
            entry = _Entry(
                client_order_id=sized.client_order_id,
                signal_id=sized.signal_id,
                symbol=sized.symbol,
                qty=sized.qty,
                submitted_at=self._clock.now(),
            )
            self._entries[entry.client_order_id] = entry
            self._in_flight += 1
        try:
            return await self._submit(entry, sized, check)
        finally:
            self._in_flight -= 1
            self._wake.set()

    async def _submit(self, entry: _Entry, intent: OrderIntent, check: RiskCheck) -> EntryResult:
        cid = entry.client_order_id
        try:
            order = await self._broker.submit_entry_oto(intent)
        except DuplicateClientOrderId:
            found = await self._broker.order_by_client_id(cid)
            if found is None:
                return await self._uncertain(entry, check)
            order = found
        except SubmitUncertain:
            # Never resubmitted: a delayed attempt could still arrive and fill (D-033).
            await self._records.mark(cid, "uncertain")
            looked_up = await _look_up(self._broker, self._clock, cid)
            if looked_up is None:
                return await self._uncertain(entry, check)
            order = looked_up
        except (OrderRejected, BrokerAuthError) as exc:
            await self._records.mark(cid, "rejected")
            self._entries.pop(cid, None)
            return EntryResult("broker_rejected", check, reason=str(exc))
        await self._adopt(entry, order)
        if self._closing:  # a halt began while this submit was in flight
            await _cancel_quietly(self._broker, order)
        return EntryResult("submitted", check, order)

    async def _uncertain(self, entry: _Entry, check: RiskCheck) -> EntryResult:
        entry.status = "uncertain"
        entry.give_up_at = self._give_up_time(entry.submitted_at)
        await self._records.mark(entry.client_order_id, "uncertain")
        return EntryResult("uncertain", check, reason="no answer: held as pending until resolved")

    def _give_up_time(self, submitted_at: datetime) -> datetime:
        return submitted_at + timedelta(seconds=self._risk.entry_timeout_s + RECONCILE_S)

    async def _adopt(self, entry: _Entry, order: Order) -> None:
        booked = await self._records.apply(order, signal_id=entry.signal_id or None)
        if order.filled_qty and entry.first_fill_at is None and not booked:
            # Fills booked earlier (e.g. before a restart): the clock starts at the first one.
            entry.first_fill_at = await self._records.first_fill_at(order.id)
        self._note(order, booked)

    def _note(self, order: Order, booked: list[BookedFill]) -> None:
        """Fold a broker snapshot of one of our entries into its tracking. A stale snapshot
        never reopens an entry the stream already saw finish."""
        entry = self._entries.get(order.client_order_id)
        if entry is None or (is_final(entry.status) and not order.final):
            return
        entry.status, entry.order_id = order.status, order.id
        entry.filled_qty = max(entry.filled_qty, order.filled_qty)
        if order.filled_qty and entry.first_fill_at is None:
            first = min((fill.at for fill in booked), default=None)
            entry.first_fill_at = first or order.filled_at or self._clock.now()

    # ---------------------------------------------------------------- what the gate needs

    async def exposure(self, intent: OrderIntent | None = None) -> Exposure:
        """One consistent read of the journal, plus the marks (D-035)."""
        today = self._clock.now().date()
        day, ledger = await self._journal.read(lambda conn: _snapshot(conn, self.book, today))
        if not any(e.ref == SEED_REF for e in ledger):
            raise LedgerNotSeededError(f"Book {self.book}'s ledger has no opening deposit")
        pending = open_risk = Decimal(0)
        symbols: set[str] = set()
        open_signals: set[str] = set()
        entered: set[str] = set()
        counted = 0
        for row, filled in day.entries:
            unconfirmed = row.status in ("submitting", "uncertain")
            is_open = unconfirmed or not (is_final(row.status) or row.status in OWN_STATUSES)
            if is_open:
                unfilled = max(row.qty - filled, 0)
                limit = row.limit_price or Decimal(0)
                pending += unfilled * limit
                if row.stop_price is not None:
                    open_risk += unfilled * (limit - row.stop_price)
                symbols.add(row.symbol)
                open_signals.add(row.signal_id or "")
            if (row.signal_id or "").startswith("smoke-"):
                continue  # smoke tests never count as strategy entries (D-035)
            if filled or is_open:
                counted += 1
            if row.status not in ("rejected", "lost"):
                entered.add(row.symbol)
        marked = Decimal(0)
        for symbol, (qty, average) in day.held.items():
            symbols.add(symbol)
            stop = await self._records.planned_stop(symbol)
            if stop is not None:
                open_risk += qty * max(average - stop, Decimal(0))
            last = self._prices.last_price(symbol)
            marked += qty * (last if last is not None else average)
        used = intent is not None and await self._records.order(intent.client_order_id) is not None
        return Exposure(
            symbols=frozenset(symbols),
            entries_today=counted,
            symbols_entered_today=frozenset(entered),
            available_cash=available(ledger, today, pending),
            equity_sod=self.equity_sod,
            realized_pnl_today=day.realized,
            open_risk=open_risk,
            ledger_equity=cash(ledger, include_fee_estimates=False) + marked,
            unprotected=frozenset(self._bare),
            client_order_id_used=used,
            open_order_for_signal=intent is not None and intent.signal_id in open_signals,
        )

    @property
    def accepting(self) -> bool:
        """False once a flatten began: the book takes no more entries today (D-044)."""
        return not (self._closing or self._flattening)

    @property
    def held(self) -> dict[str, int]:
        """Shares held per symbol, as the monitor last read them from the broker."""
        view = self._view
        return {} if view is None else {s: p.qty for s, p in sorted(view.positions.items())}

    async def cancel_entry(self, client_order_id: str) -> bool:
        """Cancel one of our open entries (its OTO leg goes with it). Returns False if the
        broker no longer has it open."""
        row = await self._records.order(client_order_id)
        if row is None or row.broker_order_id is None:
            return False
        order = await self._broker.get_order(row.broker_order_id)
        if order.final:
            return False
        await _cancel_quietly(self._broker, order)
        entry = self._entries.get(client_order_id)
        if entry is not None:
            entry.cancel_requested = True
        self._last_refresh = None
        self._wake.set()
        return True

    # ---------------------------------------------------------------- flatten

    async def flatten(
        self, *, purpose: ExitPurpose, deadline: datetime | None = None
    ) -> FlattenResult:
        """Stop new entries, let submits in flight land, then flatten the book (D-036). A
        submit that lands after this started cancels its own order.

        One flatten runs at a time: a second caller (the kill switch, the orchestrator's
        FLATTEN state, a P1 from the monitor) waits for the first, then runs its own pass,
        which finds the book flat at once if the first succeeded (D-044)."""
        self._closing = True
        async with self._flatten_lock:
            drain_until = self._clock.now() + timedelta(seconds=SUBMIT_DRAIN_S)
            while self._in_flight and self._clock.now() < drain_until:
                await self._clock.sleep_until(self._clock.now() + DRAIN_STEP)
            self._flattening = True
            self._flattens += 1
            try:
                result = await flatten_account(
                    self._broker,
                    self._records,
                    clock=self._clock,
                    deadline=deadline or self._clock.now() + timedelta(seconds=FLATTEN_TIMEOUT_S),
                    hours=self._day.hours,
                    purpose=purpose,
                )
            finally:
                self._flattening = False
                self._last_refresh = None
            if result.flat:
                self._bare.clear()
            return result

    # ---------------------------------------------------------------- the loops

    async def _consume(self) -> None:
        async for event in self._broker.trade_updates():
            try:
                if isinstance(event, StreamResync):
                    self._resync = True  # missed events are never replayed
                elif await self._records.is_ours(event.order):
                    booked = await self._records.apply_update(event)
                    self._note(event.order, booked)
                    self._last_refresh = None
                else:
                    # Someone else's order in this account: never adopt it, reconcile (D-037).
                    log.warning("foreign order", book=self.book, order=event.order.client_order_id)
                    self._resync = True
            except Exception:
                # Never lose the stream over one event: a resync re-reads it over REST.
                log.exception("trade update failed", book=self.book)
                self._resync = True
            self._wake.set()

    async def _monitor(self) -> None:
        """Protection must outlive any single failure: every error is logged and the loop
        goes on, so a bad payload can't silently end it."""
        while True:
            try:
                await self._tick()
            except BrokerError as exc:
                log.warning("execution tick failed", book=self.book, error=str(exc))
            except Exception:
                log.exception("execution tick crashed", book=self.book)
            await self._sleep(self._next_wake())

    def _busy(self) -> bool:
        return bool(self._bare or self._stop_pending or any(e.open for e in self._entries.values()))

    def _next_wake(self) -> datetime:
        now = self._clock.now()
        wake = now + timedelta(seconds=ENTRY_POLL_S if self._busy() else PROTECTION_REFRESH_S)
        deadlines = [since + self._unprotected_max for since in self._bare.values()]
        if self._confirm_at is not None:
            deadlines.append(self._confirm_at)
        return min([wake, *(d for d in deadlines if d > now)])

    async def _sleep(self, until: datetime) -> None:
        """Until the clock reaches ``until``, or something wakes the loop sooner."""
        if not self._wake.is_set():
            sleeper = asyncio.ensure_future(self._clock.sleep_until(until))
            waker = asyncio.ensure_future(self._wake.wait())
            try:
                await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                sleeper.cancel()
                waker.cancel()
        self._wake.clear()

    @property
    def _unprotected_max(self) -> timedelta:
        return timedelta(seconds=self._risk.unprotected_position_max_s)

    async def _tick(self) -> None:
        now = self._clock.now()
        if self._flattening or await self._stood_down():
            return
        await self._poll_entries(now)
        if self._refresh_due(now):
            await self._refresh_view()
        view = self._view
        if view is not None:
            flattens = self._flattens
            for position in list(view.positions.values()):
                await self._protect(position, view, now)
                if self._flattens != flattens:
                    return  # a P1 flattened the book: this view is stale
            await self._check_daily_loss(view)
        await self._maybe_reconcile(now)

    async def _stood_down(self) -> bool:
        """`trader flatten` is working on this book: leave its stops alone (D-036)."""
        return any(h.reason == "flattening" for h in await self._halts.active(self.book))

    # ---------------------------------------------------------------- entry tracking

    async def _poll_entries(self, now: datetime) -> None:
        timeout = timedelta(seconds=self._risk.entry_timeout_s)
        for entry in list(self._entries.values()):
            if entry.status == "submitting":
                continue  # its submit is still in flight
            if entry.status == "uncertain":
                await self._chase(entry, now)
                continue
            if entry.order_id is None:
                continue
            order = await self._broker.get_order(entry.order_id)
            await self._adopt(entry, order)
            if order.final:
                del self._entries[entry.client_order_id]
                continue
            unfilled_too_long = not order.filled_qty and now >= entry.submitted_at + timeout
            partial_done_waiting = (
                order.filled_qty > 0
                and entry.first_fill_at is not None
                and now >= entry.first_fill_at + timedelta(seconds=PARTIAL_FILL_GRACE_S)
            )
            if (unfilled_too_long or partial_done_waiting or self._closing) and (
                not entry.cancel_requested
            ):
                await _cancel_quietly(self._broker, order)
                entry.cancel_requested = True
                self._last_refresh = None

    async def _chase(self, entry: _Entry, now: datetime) -> None:
        """An uncertain entry: look for it each tick; call it lost after its deadline."""
        found = await self._broker.order_by_client_id(entry.client_order_id)
        if found is None:
            if entry.give_up_at is not None and now >= entry.give_up_at:
                await self._records.mark(entry.client_order_id, "lost")
                del self._entries[entry.client_order_id]
            return
        log.warning("uncertain entry found", client_order_id=entry.client_order_id)
        await self._adopt(entry, found)
        self._last_refresh = None

    # ---------------------------------------------------------------- protection (D-032)

    def _refresh_due(self, now: datetime) -> bool:
        if self._last_refresh is None:
            return True
        every = ENTRY_POLL_S if self._busy() else PROTECTION_REFRESH_S
        return now - self._last_refresh >= timedelta(seconds=every)

    async def _refresh_view(self) -> None:
        positions = {p.symbol: p for p in await self._broker.positions() if p.qty != 0}
        open_orders = await self._broker.open_orders()  # read after the positions
        self._view = _View(positions=positions, open_orders=open_orders)
        self._last_refresh = self._clock.now()
        for symbol in list(self._bare):
            if symbol not in positions:
                del self._bare[symbol]
                self._stop_pending.discard(symbol)

    async def _protect(self, position: Position, view: _View, now: datetime) -> None:
        symbol, qty = position.symbol, position.qty
        if qty <= 0:
            return  # never ours: reconciliation reports it
        covered = sum(
            order.qty - order.filled_qty
            for order in view.open_orders
            if order.symbol == symbol and self._stop_works(order, view)
        )
        if covered >= qty:
            if self._bare.pop(symbol, None) is not None and symbol in self._partial:
                self._partial.discard(symbol)
                await self._incident(
                    "P3", "partial_fill_protected", f"{symbol}: partial fill now under a stop"
                )
            self._covered_once.add(symbol)
            self._stop_pending.discard(symbol)
            return
        since = self._bare.get(symbol)
        if since is None:
            since = self._bare[symbol] = await self._bare_since(symbol, now)
        buys = [o for o in view.open_orders if o.symbol == symbol and o.side == "buy"]
        if buys:
            self._partial.add(symbol)  # its leg can't work until the buy is final
            await self._cut_short(buys, now)
        elif symbol not in self._stop_pending and now < since + self._unprotected_max:
            await self._cover(position, covered)
        if now >= since + self._unprotected_max:
            await self._fail_protection(
                symbol, f"{symbol}: {qty - covered} shares without a working stop since {since}"
            )

    @staticmethod
    def _stop_works(order: Order, view: _View) -> bool:
        """A sell stop at work. A held leg works only once its entry is final, which for a
        leg still open means filled: a cancelled entry takes its legs with it."""
        if order.side != "sell" or order.type not in ("stop", "stop_limit"):
            return False
        if order.status in WORKING_STOP_STATUSES:
            return True
        if order.status != "held" or order.group_id is None:
            return False
        return not any(o.group_id == order.group_id and o.side == "buy" for o in view.open_orders)

    async def _bare_since(self, symbol: str, now: datetime) -> datetime:
        """When the shares lost (or never had) a stop. Shares never seen covered count from
        the broker's first fill, so a restart acts at once (D-032)."""
        if symbol in self._covered_once:
            return now
        row = await self._journal.fetch_one(
            "SELECT MIN(filled_at) AS first FROM fills WHERE book = ? AND symbol = ? "
            "AND side = 'buy' AND filled_at LIKE ?",
            (self.book, symbol, f"{now.date().isoformat()}T%"),
        )
        first = parse_et_iso(row["first"]) if row is not None and row["first"] else now
        return min(first, now)

    async def _cut_short(self, buys: list[Order], now: datetime) -> None:
        """A partly filled entry gets the grace period, then its remainder is cancelled."""
        grace = timedelta(seconds=PARTIAL_FILL_GRACE_S)
        for order in buys:
            entry = self._entries.get(order.client_order_id)
            if entry is not None and (entry.cancel_requested or entry.first_fill_at is None):
                continue
            first = entry.first_fill_at if entry is not None else None
            if first is None or now >= first + grace:
                await _cancel_quietly(self._broker, order)
                if entry is not None:
                    entry.cancel_requested = True
                self._last_refresh = None

    async def _cover(self, position: Position, covered: int) -> None:
        """Give bare shares a stop, or sell them if the price is already through it."""
        symbol = position.symbol
        stop = await self._records.planned_stop(symbol)
        if stop is None:
            await self._fail_protection(symbol, f"{symbol}: no journaled plan, so no stop")
            return
        need = min(position.qty - covered, position.qty_available)
        if need <= 0:
            return
        last = self._prices.last_price(symbol)
        if last is not None and last <= stop:  # R§5 step 1: already through the stop
            await _sell(self._broker, self._records, self._clock, position, "stop_breached")
            self._last_refresh = None
            return
        cid = await self._records.pending_sell(
            symbol=symbol, qty=need, leg="stop", purpose="protective_stop", stop_price=stop
        )
        try:
            order = await self._broker.submit_stop(symbol, need, stop, cid)
        except DuplicateClientOrderId:
            found = await self._broker.order_by_client_id(cid)
            if found is None:
                return
            order = found
        except SubmitUncertain:
            looked_up = await _look_up(self._broker, self._clock, cid)
            if looked_up is None:
                await self._records.mark(cid, "lost")
                return
            order = looked_up
        except (OrderRejected, BrokerAuthError) as exc:
            await self._records.mark(cid, "rejected")
            log.warning("protective stop rejected", symbol=symbol, reason=str(exc))
            return
        await self._records.apply(order)
        self._stop_pending.add(symbol)
        self._last_refresh = None

    async def _fail_protection(self, symbol: str, message: str) -> None:
        """No stop in time: flatten, and keep flattening until flat (RISK.md §5). The P1 and
        its halt are recorded once per symbol."""
        if symbol in self._p1_symbols:
            await self.flatten(purpose="incident")
            return
        self._p1_symbols.add(symbol)
        await self._p1("unprotected_position", message)

    async def _p1(self, code: str, message: str, detail: dict[str, object] | None = None) -> None:
        """RISK.md §11: a P1 flattens the affected book and halts it until a human resumes."""
        await self._incident("P1", code, message, detail)
        await self._halts.halt(self.book, "p1_incident", detail=message)
        await self.flatten(purpose="incident")

    async def _incident(
        self, severity: Severity, code: str, message: str, detail: dict[str, object] | None = None
    ) -> None:
        await record_incident(
            self._journal,
            at=self._clock.now(),
            severity=severity,
            component=f"execution.{self.book}",
            code=code,
            message=message,
            detail=detail,
        )

    # ---------------------------------------------------------------- breakers

    async def _check_daily_loss(self, view: _View) -> None:
        """RISK.md §5: realized plus mark-to-market loss at the limit flattens the book and
        halts it until the next session."""
        if self._daily_loss_hit or self.equity_sod <= 0:
            return
        today = self._clock.now().date()
        day = await self._journal.read(lambda conn: read_book_day(conn, self.book, today))
        unrealized = Decimal(0)
        for position in view.positions.values():
            last = self._prices.last_price(position.symbol) or position.current_price
            if last is not None:
                unrealized += position.qty * (last - position.avg_entry_price)
        limit = self._risk.daily_loss_limit_pct
        if not daily_loss_breached(day.realized, unrealized, self.equity_sod, limit):
            return
        self._daily_loss_hit = True
        message = f"realized {day.realized} + unrealized {unrealized} reached {limit}% of E"
        await self._halts.halt(
            self.book, "daily_loss", detail=message, expires_at=self._day.next_open
        )
        await self._incident("P2", "daily_loss_limit", message)
        await self.flatten(purpose="daily_loss")

    # ---------------------------------------------------------------- reconciliation (D-037)

    async def _maybe_reconcile(self, now: datetime) -> None:
        if self._confirm_at is not None:
            if now < self._confirm_at:
                return  # the confirming pass waits its turn, whatever else asks for one
        elif not (self._resync or now >= self._next_reconcile):
            return
        if self._in_flight or self._flattening:
            return
        self._resync = False
        self._next_reconcile = now + timedelta(seconds=RECONCILE_S)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        found = await self._reconciler.check(since=start)
        self._last_refresh = None
        if found.empty:
            self._confirm_at = self._reported = None
            return
        if self._confirm_at is None:  # first sighting: look again shortly
            self._confirm_at = now + timedelta(seconds=RECONCILE_CONFIRM_S)
            log.warning("reconcile difference; confirming", book=self.book, diff=str(found))
            return
        self._confirm_at = None
        await self._mismatch(found)

    async def _mismatch(self, found: ReconcileDiff) -> None:
        """RISK.md §5: halt both books. Positions that differ are a P1, which also flattens
        this book (R§11); orders alone are a P2. A mismatch that persists is reported once
        (D-048), but a position mismatch keeps being flattened until it clears."""
        detail: dict[str, object] = {
            "unknown_positions": found.unknown_positions,
            "qty_mismatches": {s: list(q) for s, q in found.qty_mismatches.items()},
            "unknown_orders": list(found.unknown_orders),
        }
        key = json.dumps(detail, sort_keys=True)
        if key == self._reported:
            if found.positions_differ:
                await self.flatten(purpose="incident")
            return
        self._reported = key
        message = f"Book {self.book} disagrees with the broker: {detail}"
        for book in self._both_books:
            await self._halts.halt(book, "reconcile_mismatch", detail=message)
        if found.positions_differ:
            await self._p1("reconcile_mismatch", message, detail)
        else:
            await self._incident("P2", "reconcile_mismatch", message, detail)
