"""SimBroker: a deterministic in-memory paper account for tests and replay (A§4.6).

It follows the Alpaca behavior the execution service must survive (DECISIONS D-030, D-032):
- an OTO stop leg stays ``held`` until its entry has filled completely, and dies with it;
- wash-trade protection: an open buy blocks sells on the symbol, an open sell stop blocks buys;
- a working sell order holds its shares, so selling them again fails with "insufficient qty";
- 1x buying power, with open buys reserving it; no shorting;
- day orders expire when ``expire_day_orders`` runs; a sell after the close is only queued.

Fills follow A§4.6: a limit buy fills at ``min(limit, ask_or_last + slippage)`` once the quote or
a trade is at or below the limit; a stop triggers on a trade at or below it and fills at
``min(stop, trade) - slippage``; a market sell fills at ``bid_or_last - slippage``.

Faults are injected through the public ``fail_next_submit``, ``partial_next``, ``slow_cancels``
and the stream controls, so every scenario is reproducible under SimClock.

Alpaca paper at the open (DECISIONS D-053), each opt-in:
- ``accept_delay``: a new order waits in ``pending_new`` (it can't fill) until accepted;
- a cancel sent before acceptance is queued and processed ``queued_cancel_delay`` after it
  (``pending_new_cancels="drop"`` never processes it); meanwhile the order shows ``new``;
- a cancel that reaches an order already final comes back ``order_cancel_rejected``.
``cancels`` lists every cancel call's order id.
"""

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from trader.core.clock import Clock
from trader.core.models import OrderIntent, Quote, Trade
from trader.execution.broker import (
    AccountSnapshot,
    BrokerEvent,
    DuplicateClientOrderId,
    Order,
    OrderNotCancelable,
    OrderNotFound,
    OrderRejected,
    Position,
    Side,
    StopTif,
    StreamResync,
    SubmitUncertain,
    TradeUpdate,
    is_final,
)
from trader.execution.orders import (
    MIN_STOP_GAP,
    InvalidOrderError,
    entry_oto_body,
    market_sell_body,
    stop_body,
)

INSUFFICIENT = 40310000
DUPLICATE = 40010001
SUB_PENNY = 42210000
WASH_TRADE = "potential wash trade detected. use complex orders"

SubmitFault = Literal["reject", "timeout_before", "timeout_after", "timeout_late"]


@dataclass
class _SimOrder:
    id: str
    client_order_id: str
    symbol: str
    side: Side
    type: str
    order_class: str
    time_in_force: str
    qty: int
    limit_price: Decimal | None
    stop_price: Decimal | None
    status: str
    submitted_at: datetime
    updated_at: datetime
    group_id: str | None = None
    group_role: str | None = None
    parent_id: str | None = None
    leg_ids: list[str] = field(default_factory=list)
    filled_qty: int = 0
    filled_notional: Decimal = Decimal(0)
    filled_at: datetime | None = None
    cancel_at: datetime | None = None  # a slow cancel completes at this time
    accept_at: datetime | None = None  # not yet at the venue: accepted at this time
    queued_cancel: bool = False  # a cancel arrived before acceptance
    queued_cancel_at: datetime | None = None  # ...and is processed at this time

    @property
    def remaining(self) -> int:
        return self.qty - self.filled_qty

    @property
    def avg_price(self) -> Decimal | None:
        return self.filled_notional / self.filled_qty if self.filled_qty else None


@dataclass
class _Pending:
    """A submit whose order reaches the broker only at ``visible_at`` (fault "timeout_late")."""

    visible_at: datetime
    create: Callable[[], object]


class SimBroker:
    def __init__(
        self,
        *,
        clock: Clock,
        book: str = "A",
        cash: Decimal = Decimal("1000"),
        slippage: Decimal = Decimal("0.01"),
        partial_fill_rate: float = 0.0,
        seed: int = 0,
        leg_status_after_fill: Literal["new", "held"] = "new",
        market_open: Callable[[datetime], bool] = lambda _now: True,
    ) -> None:
        self.name = f"sim:{book}"
        self._clock, self._slippage = clock, slippage
        self._rng, self._partial_rate = random.Random(seed), partial_fill_rate
        self._leg_status_after_fill = leg_status_after_fill
        self._market_open = market_open
        self.cash = cash
        self._positions: dict[str, tuple[int, Decimal]] = {}  # symbol -> (qty, total cost)
        self._orders: dict[str, _SimOrder] = {}
        self._by_cid: dict[str, str] = {}
        self._last_trade: dict[str, Trade] = {}
        self._last_quote: dict[str, Quote] = {}
        self._events: asyncio.Queue[BrokerEvent] = asyncio.Queue()
        self._held_events: list[BrokerEvent] = []
        self._stream_paused = False
        self._pending: list[_Pending] = []
        self._ids = 0
        self._executions = 0
        # Faults, each consumed by the next matching call.
        self._submit_fault: tuple[SubmitFault, str, timedelta] | None = None
        self._partial_next: int | None = None
        self.reject_stop_legs = False
        self.reject_next_stop = False
        self.slow_cancels: timedelta | None = None
        self.accept_delay: timedelta | None = None
        self.pending_new_cancels: Literal["queue", "drop"] = "queue"
        self.queued_cancel_delay = timedelta(0)
        self.submits: list[str] = []  # client_order_ids in submission order, faults included
        self.cancels: list[str] = []  # order ids, one per cancel call

    # ---------------------------------------------------------------- fault injection

    def fail_next_submit(
        self, fault: SubmitFault, reason: str = "rejected", *, delay: timedelta = timedelta(0)
    ) -> None:
        """``reject``: refused. ``timeout_before``: never arrived. ``timeout_after``: accepted
        but the answer was lost. ``timeout_late``: arrives ``delay`` later, answer lost."""
        self._submit_fault = (fault, reason, delay)

    def partial_next(self, qty: int) -> None:
        """The next fill of any order fills only ``qty`` shares."""
        self._partial_next = qty

    def pause_stream(self) -> None:
        """Events stop reaching the stream, as on a half-dead socket."""
        self._stream_paused = True

    def resume_stream(self, *, drop: bool = False, reverse: bool = False) -> None:
        """Release the held events (``reverse``: out of order), or ``drop`` them and resync,
        as after a reconnect."""
        self._stream_paused = False
        held, self._held_events = self._held_events, []
        if drop:
            self._events.put_nowait(StreamResync(at=self._clock.now()))
            return
        for event in reversed(held) if reverse else held:
            self._events.put_nowait(event)

    @property
    def idle(self) -> bool:
        """Every event so far has been taken off the stream."""
        return self._events.empty()

    def last_price(self, symbol: str) -> Decimal | None:
        trade = self._last_trade.get(symbol)
        return None if trade is None else trade.price

    # ---------------------------------------------------------------- market data

    def feed(self, event: Trade | Quote) -> None:
        """A market event: remember it and fill whatever it makes fillable."""
        self._advance()
        if isinstance(event, Trade):
            self._last_trade[event.symbol] = event
        else:
            self._last_quote[event.symbol] = event
        self._match(event.symbol, trade=event if isinstance(event, Trade) else None)

    def hold(self, symbol: str, qty: int, price: Decimal) -> None:
        """Start with ``qty`` shares bought at ``price`` earlier (cash already spent): a
        position carried overnight, or a replay's starting state."""
        held, cost = self._positions.get(symbol, (0, Decimal(0)))
        self._positions[symbol] = (held + qty, cost + qty * price)
        self.cash -= qty * price

    def expire_day_orders(self) -> None:
        """The session closed: every open day order expires, held legs included."""
        self._advance()
        for order in list(self._orders.values()):
            if not is_final(order.status) and order.time_in_force == "day":
                self._set_status(order, "expired", "expired")

    # ---------------------------------------------------------------- reads

    async def account(self) -> AccountSnapshot:
        self._advance()
        marked = (qty * self._mark(sym, cost / qty) for sym, (qty, cost) in self._positions.items())
        equity = self.cash + sum(marked, Decimal(0))
        return AccountSnapshot(
            status="ACTIVE",
            cash=self.cash,
            equity=equity,
            buying_power=self.cash - self._reserved_buys(),
            multiplier=Decimal(1),
            is_paper=True,
        )

    async def positions(self) -> list[Position]:
        self._advance()
        return [
            Position(
                symbol=symbol,
                qty=qty,
                qty_available=max(0, qty - self._reserved_sells(symbol)),
                avg_entry_price=cost / qty,
                current_price=self._mark(symbol, cost / qty),
            )
            for symbol, (qty, cost) in sorted(self._positions.items())
        ]

    async def open_orders(self) -> list[Order]:
        """Every order that is not final, legs listed on their own (Alpaca's nested=false)."""
        self._advance()
        return [
            self._snapshot(o).model_copy(update={"legs": ()})
            for o in self._orders.values()
            if not is_final(o.status)
        ]

    async def get_order(self, order_id: str) -> Order:
        self._advance()
        if order_id not in self._orders:
            raise OrderNotFound(order_id)
        return self._snapshot(self._orders[order_id])

    async def order_by_client_id(self, client_order_id: str) -> Order | None:
        self._advance()
        order_id = self._by_cid.get(client_order_id)
        return None if order_id is None else self._snapshot(self._orders[order_id])

    async def orders_since(self, after: datetime) -> list[Order]:
        self._advance()
        return [
            self._snapshot(o)
            for o in self._orders.values()
            if o.parent_id is None and o.submitted_at > after
        ]

    # ---------------------------------------------------------------- order mutations

    async def submit_entry_oto(self, intent: OrderIntent) -> Order:
        self._advance()
        cid = intent.client_order_id
        fault = self._take_fault(cid)
        try:
            entry_oto_body(intent)
        except InvalidOrderError as exc:
            raise OrderRejected(str(exc), status=422, code=SUB_PENNY) from None
        self._check_new_cid(cid)
        symbol, limit, stop = intent.symbol, intent.entry_limit, intent.stop_price
        current = self._ask_or_last(symbol)
        if current is not None and stop > current - MIN_STOP_GAP:
            raise OrderRejected(
                "stop price must be at least 0.01 below the current price", status=422
            )
        if self._open(symbol, side="sell"):
            raise OrderRejected(WASH_TRADE, status=403)
        if intent.qty * limit > self.cash - self._reserved_buys():
            raise OrderRejected("insufficient buying power", status=403, code=INSUFFICIENT)

        def create() -> Order:
            now = self._clock.now()
            group = self._new_id("grp")
            entry = self._add(
                _SimOrder(
                    id=self._new_id("ord"),
                    client_order_id=cid,
                    symbol=symbol,
                    side="buy",
                    type="limit",
                    order_class="oto",
                    time_in_force="day",
                    qty=intent.qty,
                    limit_price=limit,
                    stop_price=None,
                    status="new",
                    submitted_at=now,
                    updated_at=now,
                    group_id=group,
                    group_role="entry",
                )
            )
            leg = self._add(
                _SimOrder(
                    id=self._new_id("ord"),
                    client_order_id=self._new_id("leg"),
                    symbol=symbol,
                    side="sell",
                    type="stop",
                    order_class="oto",
                    time_in_force="day",
                    qty=intent.qty,
                    limit_price=None,
                    stop_price=stop,
                    status="held",
                    submitted_at=now,
                    updated_at=now,
                    group_id=group,
                    group_role="stop_loss",
                    parent_id=entry.id,
                )
            )
            entry.leg_ids.append(leg.id)
            arrived = self._arrive(entry)
            snapshot = self._snapshot(entry)
            self._emit(arrived, entry)
            self._match(symbol)
            return snapshot

        return self._place(fault, create)

    async def submit_stop(
        self,
        symbol: str,
        qty: int,
        stop_price: Decimal,
        client_order_id: str,
        *,
        tif: StopTif = "day",
    ) -> Order:
        self._advance()
        fault = self._take_fault(client_order_id)
        stop_body(symbol, qty, stop_price, client_order_id, tif)
        self._check_new_cid(client_order_id)
        if self.reject_next_stop:
            self.reject_next_stop = False
            raise OrderRejected("stop rejected (injected)", status=403)
        self._check_sell(symbol, qty)

        def create() -> Order:
            order = self._new_sell(symbol, qty, client_order_id, "stop", tif, stop_price)
            arrived = self._arrive(order)
            snapshot = self._snapshot(order)
            self._emit(arrived, order)
            last = self._last_trade.get(symbol)
            if last is not None and last.price <= stop_price:
                self._match(symbol, trade=last)  # already through the stop: it triggers
            return snapshot

        return self._place(fault, create)

    async def submit_market_sell(self, symbol: str, qty: int, client_order_id: str) -> Order:
        self._advance()
        fault = self._take_fault(client_order_id)
        market_sell_body(symbol, qty, client_order_id)
        self._check_new_cid(client_order_id)
        self._check_sell(symbol, qty)

        def create() -> Order:
            order = self._new_sell(symbol, qty, client_order_id, "market", "day", None)
            arrived = "new"
            if not self._market_open(self._clock.now()):
                order.status = "accepted"  # queued for the next session (Alpaca, after 16:00)
            else:
                arrived = self._arrive(order)
            snapshot = self._snapshot(order)
            self._emit(arrived, order)
            self._match(symbol)
            return snapshot

        return self._place(fault, create)

    async def cancel(self, order_id: str) -> None:
        self._advance()
        self.cancels.append(order_id)
        order = self._orders.get(order_id)
        if order is None:
            raise OrderNotFound(order_id)
        if is_final(order.status):
            raise OrderNotCancelable(f"{order_id} is already {order.status}")
        if order.status == "pending_cancel":
            return
        if order.accept_at is not None:  # not at the venue yet: queued until accepted
            order.status, order.queued_cancel = "pending_cancel", True
            self._emit("pending_cancel", order)
            return
        if self.slow_cancels is not None:
            order.status = "pending_cancel"
            order.cancel_at = self._clock.now() + self.slow_cancels
            self._emit("pending_cancel", order)
            return
        self._cancel_group(order)

    # ---------------------------------------------------------------- the stream

    async def trade_updates(self) -> AsyncIterator[BrokerEvent]:
        yield StreamResync(at=self._clock.now())  # like a first connection
        while True:
            yield await self._events.get()

    async def aclose(self) -> None:
        return None

    # ---------------------------------------------------------------- internals: orders

    def _place(
        self, fault: tuple[SubmitFault, str, timedelta] | None, create: Callable[[], Order]
    ) -> Order:
        if fault is None:
            return create()
        kind, reason, delay = fault
        if kind == "reject":
            raise OrderRejected(reason, status=403)
        if kind == "timeout_after":
            create()
        elif kind == "timeout_late":
            self._pending.append(_Pending(self._clock.now() + delay, create))
        raise SubmitUncertain(f"injected {kind}")

    def _take_fault(self, cid: str) -> tuple[SubmitFault, str, timedelta] | None:
        self.submits.append(cid)
        fault, self._submit_fault = self._submit_fault, None
        return fault

    def _check_new_cid(self, cid: str) -> None:
        if cid in self._by_cid:
            raise DuplicateClientOrderId(
                "client_order_id must be unique", status=422, code=DUPLICATE
            )

    def _check_sell(self, symbol: str, qty: int) -> None:
        if self._open(symbol, side="buy"):
            raise OrderRejected(WASH_TRADE, status=403)
        held, _ = self._positions.get(symbol, (0, Decimal(0)))
        available = held - self._reserved_sells(symbol)
        if qty > available:
            raise OrderRejected(
                f"insufficient qty available for order (requested: {qty}, available: "
                f"{max(available, 0)})",
                status=403,
                code=INSUFFICIENT,
            )

    def _new_sell(
        self,
        symbol: str,
        qty: int,
        cid: str,
        kind: str,
        tif: str,
        stop: Decimal | None,
    ) -> _SimOrder:
        now = self._clock.now()
        return self._add(
            _SimOrder(
                id=self._new_id("ord"),
                client_order_id=cid,
                symbol=symbol,
                side="sell",
                type=kind,
                order_class="simple",
                time_in_force=tif,
                qty=qty,
                limit_price=None,
                stop_price=stop,
                status="new",
                submitted_at=now,
                updated_at=now,
            )
        )

    def _add(self, order: _SimOrder) -> _SimOrder:
        self._orders[order.id] = order
        self._by_cid[order.client_order_id] = order.id
        return order

    def _cancel_group(self, order: _SimOrder) -> None:
        """Cancel ``order`` and, as Alpaca does, every other open order in its OTO group."""
        group = [order]
        if order.group_id is not None:
            group = [o for o in self._orders.values() if o.group_id == order.group_id]
        for member in group:
            if not is_final(member.status):
                self._set_status(member, "canceled", "canceled")

    def _set_status(self, order: _SimOrder, status: str, event: str) -> None:
        order.status = status
        order.updated_at = self._clock.now()
        self._emit(event, order)

    def _open(self, symbol: str, *, side: Side) -> bool:
        return any(
            o.symbol == symbol and o.side == side and not is_final(o.status)
            for o in self._orders.values()
        )

    def _reserved_buys(self) -> Decimal:
        return sum(
            (
                o.remaining * (o.limit_price or Decimal(0))
                for o in self._orders.values()
                if o.side == "buy" and not is_final(o.status)
            ),
            Decimal(0),
        )

    def _reserved_sells(self, symbol: str) -> int:
        return sum(
            o.remaining
            for o in self._orders.values()
            if o.symbol == symbol and o.side == "sell" and not is_final(o.status)
        )

    # ---------------------------------------------------------------- internals: matching

    def _match(self, symbol: str, trade: Trade | None = None) -> None:
        for order in list(self._orders.values()):
            if order.symbol != symbol or is_final(order.status):
                continue
            price = self._fill_price(order, trade)
            if price is not None:
                self._fill(order, price)

    def _fill_price(self, order: _SimOrder, trade: Trade | None) -> Decimal | None:
        if order.accept_at is not None or order.status in ("held", "accepted"):
            return None  # not at the venue yet, an inactive leg, or queued for the next session
        if order.side == "buy":
            # A trade at or below the limit fills it even while the quoted ask is higher.
            reference = trade.price if trade is not None else self._ask_or_last(order.symbol)
            limit = order.limit_price
            if reference is None or limit is None or reference > limit:
                return None
            return min(limit, reference + self._slippage)
        if order.type == "market":
            bid = self._bid_or_last(order.symbol)
            return None if bid is None else bid - self._slippage
        stop = order.stop_price
        if trade is None or stop is None or trade.price > stop:
            return None
        return min(stop, trade.price) - self._slippage

    def _fill(self, order: _SimOrder, price: Decimal) -> None:
        qty = order.remaining
        if self._partial_next is not None:
            qty, self._partial_next = min(qty, self._partial_next), None
        elif qty > 1 and self._rng.random() < self._partial_rate:
            qty = self._rng.randint(1, qty - 1)
        now = self._clock.now()
        order.filled_qty += qty
        order.filled_notional += qty * price
        order.filled_at = now if order.remaining == 0 else order.filled_at
        if order.remaining == 0:
            order.status = "filled"
        elif order.status != "pending_cancel":  # a pending cancel survives a partial fill
            order.status = "partially_filled"
        order.updated_at = now
        held, cost = self._positions.get(order.symbol, (0, Decimal(0)))
        if order.side == "buy":
            self.cash -= qty * price
            held, cost = held + qty, cost + qty * price
        else:
            self.cash += qty * price
            cost = cost - cost * qty / held if held else Decimal(0)
            held -= qty
        if held:
            self._positions[order.symbol] = (held, cost)
        else:
            self._positions.pop(order.symbol, None)
        self._executions += 1
        self._emit(
            "fill" if order.remaining == 0 else "partial_fill",
            order,
            execution_id=f"sim-exec-{self._executions}",
            qty=qty,
            price=price,
            position_qty=held,
        )
        if order.side == "buy" and order.remaining == 0:
            self._activate_legs(order)

    def _activate_legs(self, entry: _SimOrder) -> None:
        for leg_id in entry.leg_ids:
            leg = self._orders[leg_id]
            if leg.status != "held":
                continue
            if self.reject_stop_legs:
                self._set_status(leg, "rejected", "rejected")
            elif self._leg_status_after_fill == "new":
                self._set_status(leg, "new", "new")

    # ---------------------------------------------------------------- internals: time

    def _advance(self) -> None:
        """Apply what the passage of simulated time has made due."""
        now = self._clock.now()
        due, self._pending = (
            [p for p in self._pending if p.visible_at <= now],
            [p for p in self._pending if p.visible_at > now],
        )
        for pending in due:
            pending.create()
        for order in list(self._orders.values()):
            if order.accept_at is not None and order.accept_at <= now:
                self._accept(order)
            if order.queued_cancel_at is not None and order.queued_cancel_at <= now:
                order.queued_cancel_at = None
                self._finish_cancel(order, dropped=self.pending_new_cancels == "drop")
            if order.cancel_at is not None and order.cancel_at <= now:
                order.cancel_at = None
                self._finish_cancel(order, dropped=False)

    def _accept(self, order: _SimOrder) -> None:
        """The venue accepts the order. A cancel queued meanwhile now waits its delay, while
        the order shows ``new`` and can fill (Alpaca paper, 2026-10-13)."""
        accepted_at, order.accept_at = order.accept_at, None
        if is_final(order.status):  # it ended while it waited (expired at the close, say)
            order.queued_cancel = False
            return
        if order.queued_cancel and accepted_at is not None:  # timed from the acceptance itself
            order.queued_cancel = False
            order.queued_cancel_at = accepted_at + self.queued_cancel_delay
        if (
            order.type == "market"
            and accepted_at is not None
            and not self._market_open(accepted_at)
        ):
            self._set_status(order, "accepted", "accepted")  # queued for the next session
            return
        self._set_status(order, "new", "new")
        self._match(order.symbol)

    def _finish_cancel(self, order: _SimOrder, *, dropped: bool) -> None:
        if dropped or is_final(order.status):
            self._emit("order_cancel_rejected", order)
            return
        self._cancel_group(order)

    def _arrive(self, order: _SimOrder) -> str:
        """A new order's first status and event: ``pending_new`` until accepted when
        ``accept_delay`` is set, else ``new``."""
        if self.accept_delay is None:
            return "new"
        order.status = "pending_new"
        order.accept_at = self._clock.now() + self.accept_delay
        return "pending_new"

    # ---------------------------------------------------------------- internals: helpers

    def _ask_or_last(self, symbol: str) -> Decimal | None:
        quote = self._last_quote.get(symbol)
        if quote is not None:
            return quote.ask
        trade = self._last_trade.get(symbol)
        return None if trade is None else trade.price

    def _bid_or_last(self, symbol: str) -> Decimal | None:
        quote = self._last_quote.get(symbol)
        if quote is not None:
            return quote.bid
        trade = self._last_trade.get(symbol)
        return None if trade is None else trade.price

    def _mark(self, symbol: str, fallback: Decimal) -> Decimal:
        trade = self._last_trade.get(symbol)
        return fallback if trade is None else trade.price

    def _new_id(self, prefix: str) -> str:
        self._ids += 1
        return f"sim-{prefix}-{self._ids}"

    def _snapshot(self, order: _SimOrder) -> Order:
        return Order(
            id=order.id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            type=order.type,
            order_class=order.order_class,
            time_in_force=order.time_in_force,
            qty=order.qty,
            filled_qty=order.filled_qty,
            filled_avg_price=order.avg_price,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            status=order.status,
            submitted_at=order.submitted_at,
            updated_at=order.updated_at,
            filled_at=order.filled_at,
            group_id=order.group_id,
            group_role=order.group_role,
            legs=tuple(self._snapshot(self._orders[leg_id]) for leg_id in order.leg_ids),
        )

    def _emit(
        self,
        event: str,
        order: _SimOrder,
        *,
        execution_id: str | None = None,
        qty: int | None = None,
        price: Decimal | None = None,
        position_qty: int | None = None,
    ) -> None:
        snapshot = self._snapshot(order).model_copy(update={"legs": ()})  # stream: no legs
        update = TradeUpdate(
            event=event,
            order=snapshot,
            at=self._clock.now(),
            execution_id=execution_id,
            qty=qty,
            price=price,
            position_qty=position_qty,
        )
        if self._stream_paused:
            self._held_events.append(update)
        else:
            self._events.put_nowait(update)
