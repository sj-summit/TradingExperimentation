"""Daily and opening-range indicators (STRATEGY.md §3.1-3.2). Pure functions.

ATR14 is the mean of the 14 Wilder true ranges of the sessions before t, which is Wilder's ATR
at its seed (DECISIONS D-025). Prices stay Decimal; ratios are floats (analytics only).
"""

import itertools
from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal

from trader.core.models import Bar

PERIOD = 14
ATR_QUANTUM = Decimal("0.0001")


class InsufficientHistoryError(ValueError):
    """Fewer sessions than the indicator needs."""


def true_range(high: Decimal, low: Decimal, *, prev_close: Decimal) -> Decimal:
    """Wilder's true range: the widest of high-low and the two gaps from the previous close."""
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(bars: Sequence[Bar], period: int = PERIOD) -> Decimal:
    """Mean true range of the last ``period`` bars, oldest first; needs ``period + 1`` bars."""
    if len(bars) < period + 1:
        raise InsufficientHistoryError(f"ATR{period} needs {period + 1} sessions, got {len(bars)}")
    window = bars[-(period + 1) :]
    total = sum(
        (
            true_range(cur.high, cur.low, prev_close=prev.close)
            for prev, cur in itertools.pairwise(window)
        ),
        Decimal(0),
    )
    return (total / period).quantize(ATR_QUANTUM, rounding=ROUND_HALF_EVEN)


def adv(bars: Sequence[Bar], period: int = PERIOD) -> int:
    """Average daily volume of the last ``period`` bars, floored to whole shares."""
    if len(bars) < period:
        raise InsufficientHistoryError(f"ADV{period} needs {period} sessions, got {len(bars)}")
    return sum(bar.volume for bar in bars[-period:]) // period


def or_vol_avg(volumes: Sequence[int], period: int = PERIOD) -> float:
    """Mean opening-range volume of the last ``period`` sessions (live feed)."""
    if len(volumes) < period:
        raise InsufficientHistoryError(f"OR baseline needs {period} sessions, got {len(volumes)}")
    return sum(volumes[-period:]) / period


def rel_vol(or_volume: int, or_vol_avg14: float) -> float | None:
    """Today's OR volume over its baseline; None when the baseline is zero."""
    if or_vol_avg14 <= 0:
        return None
    return or_volume / or_vol_avg14


def or_range_atr(or_high: Decimal, or_low: Decimal, atr14: Decimal) -> float:
    """Opening-range height in ATRs."""
    _positive("atr14", atr14)
    return float((or_high - or_low) / atr14)


def or_return_pct(or_open: Decimal, or_close: Decimal) -> float:
    _positive("or_open", or_open)
    return float((or_close - or_open) / or_open * 100)


def gap_pct(or_open: Decimal, prev_close: Decimal) -> float:
    """Today's open against the previous session's close, in percent."""
    _positive("prev_close", prev_close)
    return float((or_open - prev_close) / prev_close * 100)


def atr_pct(atr14: Decimal, prev_close: Decimal) -> float:
    _positive("prev_close", prev_close)
    return float(atr14 / prev_close * 100)


def _positive(name: str, value: Decimal) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
