"""Order construction (A§4.6): the request bodies.

Alpaca rejects sub-penny prices (code 42210000; the increments live in ``core/prices.py``).
Bodies send prices and quantities as exact strings, whole shares, regular hours only,
``time_in_force`` day unless a stop must outlive the session (DECISIONS D-030).
"""

from decimal import Decimal
from typing import Any

from trader.core.ids import check_client_order_id
from trader.core.models import OrderIntent
from trader.core.prices import CENT, tick_size, valid_price
from trader.execution.broker import StopTif

MIN_STOP_GAP = CENT  # an OTO stop must sit at least $0.01 below its entry limit (Alpaca)


class InvalidOrderError(ValueError):
    """The order would break Alpaca's rules or ours, so it is never sent."""


def price_text(price: Decimal) -> str:
    """The exact string Alpaca receives, e.g. ``"12.30"``."""
    if not valid_price(price):
        raise InvalidOrderError(f"{price} is not a valid price (sub-penny rules)")
    return str(price.quantize(tick_size(price)))


def entry_oto_body(intent: OrderIntent) -> dict[str, Any]:
    """A marketable-limit buy with a stop-loss leg, tif day, regular hours (S§3.3, A§4.6)."""
    if intent.side != "buy":
        raise InvalidOrderError("entries are long buys only")
    if intent.take_profit is not None:
        raise InvalidOrderError("no take-profit leg: winners run to the flatten (S§3.4)")
    _check_qty(intent.qty)
    limit, stop = intent.entry_limit, intent.stop_price
    if stop > limit - MIN_STOP_GAP:
        raise InvalidOrderError(f"stop {stop} must be at least $0.01 below the limit {limit}")
    return {
        "symbol": intent.symbol,
        "qty": str(intent.qty),
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": price_text(limit),
        "order_class": "oto",
        "stop_loss": {"stop_price": price_text(stop)},
        "client_order_id": check_client_order_id(intent.client_order_id),
        "extended_hours": False,
    }


def stop_body(
    symbol: str, qty: int, stop_price: Decimal, client_order_id: str, tif: StopTif = "day"
) -> dict[str, Any]:
    """A standalone protective sell stop."""
    _check_qty(qty)
    return {
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "stop",
        "time_in_force": tif,
        "stop_price": price_text(stop_price),
        "client_order_id": check_client_order_id(client_order_id),
    }


def market_sell_body(symbol: str, qty: int, client_order_id: str) -> dict[str, Any]:
    """An exit at market, tif day."""
    _check_qty(qty)
    return {
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": check_client_order_id(client_order_id),
    }


def _check_qty(qty: int) -> None:
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
        raise InvalidOrderError(f"quantity must be a whole number of shares >= 1, not {qty!r}")
