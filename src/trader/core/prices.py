"""Alpaca's price increments (DECISIONS D-030), shared by strategy, risk and execution.

A price at or above $1.00 may have 2 decimals, one below $1.00 may have 4; anything finer is
rejected as sub-penny (code 42210000).
"""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

ONE_DOLLAR = Decimal("1")
CENT = Decimal("0.01")
SUB_DOLLAR_TICK = Decimal("0.0001")


def tick_size(price: Decimal) -> Decimal:
    return CENT if price >= ONE_DOLLAR else SUB_DOLLAR_TICK


def floor_tick(price: Decimal) -> Decimal:
    """Round down to a valid price."""
    return _snap(price, ROUND_FLOOR)


def ceil_tick(price: Decimal) -> Decimal:
    """Round up to a valid price."""
    return _snap(price, ROUND_CEILING)


def _snap(price: Decimal, rounding: str) -> Decimal:
    snapped = price.quantize(tick_size(price), rounding=rounding)
    # Rounding up can cross $1.00 (0.99995 -> 1.0000); snap again for the new side.
    return snapped.quantize(tick_size(snapped), rounding=rounding)


def valid_price(price: Decimal) -> bool:
    return price > 0 and price == price.quantize(tick_size(price))
