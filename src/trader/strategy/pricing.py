"""Entry, stop and chase prices (S§3.3, S§3.4, S§7.2.5; DECISIONS D-040).

Pure functions shared by the scan (nominal prices), the books (the real limit at submission)
and the backtest. Every price is snapped to a valid Alpaca tick: triggers and buffered limits
round up, stops and the chase cap round down, so a limit capped at ``max_chase`` stays valid.
"""

from dataclasses import dataclass
from decimal import Decimal

from trader.config import EntryConfig, ExitConfig
from trader.core.prices import ceil_tick, floor_tick

HUNDRED = Decimal(100)


@dataclass(frozen=True)
class LongPrices:
    trigger: Decimal
    stop: Decimal
    max_chase: Decimal
    nominal_limit: Decimal  # the S§3.3 limit with last = trigger


@dataclass(frozen=True)
class ShortPrices:
    """A shadow-only short (S§7.2.5): never traded, only simulated."""

    trigger: Decimal
    stop: Decimal


def long_prices(
    or_high: Decimal,
    atr14: Decimal,
    *,
    entry: EntryConfig,
    exit: ExitConfig,
    max_chase_pct: Decimal,
) -> LongPrices:
    trigger = ceil_tick(or_high + entry.trigger_offset)
    stop = floor_tick(trigger - exit.stop_atr_fraction * atr14)
    max_chase = floor_tick(trigger * (1 + max_chase_pct / HUNDRED))
    limit = entry_limit(trigger, trigger, entry=entry, max_chase=max_chase)
    return LongPrices(trigger=trigger, stop=stop, max_chase=max_chase, nominal_limit=limit)


def short_prices(
    or_low: Decimal, atr14: Decimal, *, entry: EntryConfig, exit: ExitConfig
) -> ShortPrices:
    trigger = floor_tick(or_low - entry.trigger_offset)
    stop = ceil_tick(trigger + exit.stop_atr_fraction * atr14)
    return ShortPrices(trigger=trigger, stop=stop)


def entry_limit(
    trigger: Decimal, last: Decimal, *, entry: EntryConfig, max_chase: Decimal
) -> Decimal:
    """``min(max(ceil(max(trigger, last) x (1 + buffer)), trigger + min_buffer), max_chase)``."""
    buffered = ceil_tick(max(trigger, last) * (1 + entry.limit_buffer_pct / HUNDRED))
    return min(max(buffered, trigger + entry.min_limit_buffer), max_chase)


def is_chased(last: Decimal, max_chase: Decimal) -> bool:
    """S§3.3's chase guard: past ``max_chase`` the entry is skipped."""
    return last > max_chase
