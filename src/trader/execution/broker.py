"""The broker interface (A§4.6): data models, order-status rules, and the adapter protocol.

``AlpacaBroker`` (paper accounts) and ``SimBroker`` (tests and replay) implement
``BrokerAdapter``. Only execution/service.py calls the order methods (CLAUDE.md invariant 1).
Prices are Decimal, quantities whole shares, timestamps tz-aware ET.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from trader.core.models import OrderIntent

Side = Literal["buy", "sell"]
StopTif = Literal["day", "gtc"]

# Alpaca order statuses. An order in a final status never changes again.
FINAL_STATUSES = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})
# A sell stop in one of these statuses is working at the broker. An OTO stop leg sits in "held"
# until its entry has completely filled, and only then can it protect anything; see
# ``stop_is_working``.
WORKING_STOP_STATUSES = frozenset({"new", "accepted", "pending_new", "partially_filled"})


def is_final(status: str) -> bool:
    return status in FINAL_STATUSES


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class AccountSnapshot(_Frozen):
    status: str
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    multiplier: Decimal | None = None
    trading_blocked: bool = False
    account_blocked: bool = False
    trade_suspended_by_user: bool = False
    shorting_enabled: bool = False
    is_paper: bool


class Position(_Frozen):
    """A broker position. ``qty`` is negative for a short, which this bot never opens."""

    symbol: str
    qty: int
    qty_available: int  # not held by open orders
    avg_entry_price: Decimal
    current_price: Decimal | None = None


class Order(_Frozen):
    id: str
    client_order_id: str
    symbol: str
    side: Side
    type: str  # market | limit | stop | stop_limit | trailing_stop
    order_class: str  # "" | simple | oto | bracket | oco
    time_in_force: str
    qty: int
    filled_qty: int
    filled_avg_price: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    status: str
    submitted_at: datetime | None = None
    updated_at: datetime | None = None
    filled_at: datetime | None = None
    group_id: str | None = None  # shared by an OTO entry and its legs
    group_role: str | None = None  # entry | stop_loss | take_profit
    legs: tuple["Order", ...] = ()
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def final(self) -> bool:
        return is_final(self.status)

    @property
    def stop_leg(self) -> "Order | None":
        """The OTO stop-loss leg, when the broker returned the legs nested."""
        for leg in self.legs:
            if leg.group_role == "stop_loss" or (leg.side == "sell" and leg.type == "stop"):
                return leg
        return None


def stop_is_working(stop: Order, entry: Order | None = None) -> bool:
    """True if ``stop`` is a sell stop that protects shares right now.

    A held OTO leg counts only once its ``entry`` has completely filled: before that Alpaca
    keeps it inactive, so partially filled shares are unprotected (DECISIONS D-032).
    """
    if stop.side != "sell" or stop.type not in ("stop", "stop_limit"):
        return False
    if stop.status in WORKING_STOP_STATUSES:
        return True
    return stop.status == "held" and entry is not None and entry.status == "filled"


class TradeUpdate(_Frozen):
    """One event from the broker's trade-updates stream. ``qty`` and ``price`` describe this
    execution only; ``order.filled_qty`` is the order's cumulative fill."""

    event: str  # new, fill, partial_fill, canceled, expired, rejected, replaced, ...
    order: Order
    at: datetime | None = None
    execution_id: str | None = None
    qty: int | None = None
    price: Decimal | None = None
    position_qty: int | None = None


class StreamResync(_Frozen):
    """The trade stream (re)connected. Alpaca never replays missed events, so the consumer
    must resync orders and fills over REST."""

    at: datetime


BrokerEvent = TradeUpdate | StreamResync


# ---------------------------------------------------------------- errors


class BrokerError(Exception):
    """A broker call failed."""


class BrokerAuthError(BrokerError):
    """The keys were rejected, or are missing."""


class BrokerUnavailable(BrokerError):
    """A read failed after its retries (network errors, 5xx)."""


class BrokerDataError(BrokerError):
    """The broker returned something this bot never creates, e.g. a fractional quantity."""


class OrderRejected(BrokerError):
    """The broker definitively refused the order: it does not exist. ``code`` is Alpaca's."""

    def __init__(self, reason: str, *, status: int = 0, code: int | None = None) -> None:
        super().__init__(reason)
        self.reason, self.status, self.code = reason, status, code


class DuplicateClientOrderId(OrderRejected):
    """An order with this client_order_id already exists: look it up and adopt it."""


class SubmitUncertain(BrokerError):
    """A submit got no definite answer, so the order may exist. Look it up before acting."""


class OrderNotFound(BrokerError):
    """No order with this id."""


class OrderNotCancelable(BrokerError):
    """The broker refused a cancel (already final, or mid-replace): re-read the order."""


# ---------------------------------------------------------------- the adapter


class BrokerAdapter(Protocol):
    """One book's broker account. Reads may be retried; submits never are (A§10)."""

    name: str

    async def account(self) -> AccountSnapshot: ...
    async def positions(self) -> list[Position]: ...
    async def open_orders(self) -> list[Order]:
        """Every order that is not final, OTO legs listed on their own (not nested), so a
        working stop leg shows up even after its entry has filled."""
        ...

    async def get_order(self, order_id: str) -> Order:
        """Raises OrderNotFound."""
        ...

    async def order_by_client_id(self, client_order_id: str) -> Order | None: ...
    async def orders_since(self, after: datetime) -> list[Order]:
        """Orders submitted after ``after``, in any status, legs nested."""
        ...

    async def submit_entry_oto(self, intent: OrderIntent) -> Order:
        """A limit buy with a stop-loss leg, tif day (A§4.6). Raises OrderRejected,
        DuplicateClientOrderId or SubmitUncertain."""
        ...

    async def submit_stop(
        self,
        symbol: str,
        qty: int,
        stop_price: Decimal,
        client_order_id: str,
        *,
        tif: StopTif = "day",
    ) -> Order: ...
    async def submit_market_sell(self, symbol: str, qty: int, client_order_id: str) -> Order: ...
    async def cancel(self, order_id: str) -> None:
        """Request a cancel. Success means requested, not done: wait for the final status.
        Raises OrderNotCancelable or OrderNotFound."""
        ...

    def trade_updates(self) -> AsyncIterator[BrokerEvent]: ...
    async def aclose(self) -> None: ...
