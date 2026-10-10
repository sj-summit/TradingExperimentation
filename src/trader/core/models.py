"""Shared field types and data contracts (ARCHITECTURE.md §5); more join as phases need them.

Prices are Decimal; timestamps are tz-aware ET. Provider-specific parsing lives with each client.
"""

import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict

_TIME_RE = re.compile(r"\d{2}:\d{2}(:\d{2})?")


def _clock_time(value: object) -> object:
    if isinstance(value, time):
        return value
    if isinstance(value, str) and _TIME_RE.fullmatch(value):
        return time.fromisoformat(value)
    # YAML 1.1 reads an unquoted 15:30 as the integer 930, so only quoted strings are accepted.
    raise ValueError('must be a quoted "HH:MM" or "HH:MM:SS" string')


def _exact_decimal(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("must be a number")
    if isinstance(value, float):
        return Decimal(str(value))  # 0.3 -> Decimal("0.3"), not its binary expansion
    return value


ClockTime = Annotated[time, BeforeValidator(_clock_time)]
"""Wall-clock ET time of day, written as a quoted "HH:MM[:SS]" string."""

Dec = Annotated[Decimal, BeforeValidator(_exact_decimal)]
"""Decimal that converts YAML floats through their shortest repr, so 0.03 stays exactly 0.03."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------- market data


class Bar(_Frozen):
    """One OHLCV bar. ``ts`` is the bar's start; a daily bar starts at 00:00 ET."""

    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    trades: int | None = None
    vwap: Decimal | None = None


class Trade(_Frozen):
    symbol: str
    ts: datetime
    price: Decimal
    size: int
    conditions: tuple[str, ...] = ()


class Quote(_Frozen):
    symbol: str
    ts: datetime
    bid: Decimal
    bid_size: int
    ask: Decimal
    ask_size: int


class Snapshot(_Frozen):
    """Latest trade and quote plus the minute, daily and previous-daily bars for one symbol."""

    symbol: str
    latest_trade: Trade | None = None
    latest_quote: Quote | None = None
    minute_bar: Bar | None = None
    daily_bar: Bar | None = None
    prev_daily_bar: Bar | None = None


class Asset(_Frozen):
    symbol: str
    name: str
    exchange: str
    asset_class: str
    status: str
    tradable: bool
    attributes: tuple[str, ...] = ()


class Split(_Frozen):
    """A forward or reverse split; ``ex_date`` is the day prices trade split-adjusted."""

    symbol: str
    ex_date: date
    process_date: date | None
    old_rate: Decimal
    new_rate: Decimal
    reverse: bool


class Mover(_Frozen):
    symbol: str
    percent_change: float
    change: Decimal
    price: Decimal


# ---------------------------------------------------------------- news and filings


class NewsItem(_Frozen):
    """A news article (Benzinga via Alpaca). Text fields are third-party and stored raw;
    they are sanitized only when a context pack is built (A§4.2)."""

    id: int
    headline: str
    summary: str
    author: str
    source: str
    url: str | None
    symbols: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


class Filing(_Frozen):
    """An SEC filing of a flagged form. ``filed_at`` is EDGAR's acceptance time (ET), or None
    until it has been read from the filing header."""

    accession: str
    cik: str
    symbol: str
    form: str
    filing_date: date
    filed_at: datetime | None
    items: str
    url: str
