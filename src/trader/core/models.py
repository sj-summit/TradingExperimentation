"""Shared field types and data contracts (ARCHITECTURE.md §5); more join as phases need them.

Prices are Decimal; timestamps are tz-aware ET. Provider-specific parsing lives with each client.
"""

import re
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Any, Literal

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


# ---------------------------------------------------------------- strategy

SignalDirection = Literal["long", "shadow_short", "none"]
SignalStatus = Literal["armed", "not_armed", "triggered", "expired", "missed_at_arm"]
FeatureValue = float | int | str | bool | None


class Signal(_Frozen):
    """One top-20 row of the 09:35 scan (S§3.2, A§5; DECISIONS D-040).

    ``direction`` is ``none`` for a doji (A§5's enum had no value for it). A long starts
    ``armed``; shorts and dojis are ``not_armed``. Trigger, stop and chase prices are set for
    longs; a shadow short has a trigger and a stop only (S§7.2.5); a doji has none.
    ``entry_limit`` is the nominal limit (last = trigger); the real one is priced at submission.
    ``features`` hold pre-decision data only.
    """

    id: str
    date: date
    symbol: str
    strategy: Literal["orb5_sip_long"] = "orb5_sip_long"
    direction: SignalDirection
    status: SignalStatus
    rel_vol: float
    rel_vol_rank: int
    or_open: Decimal
    or_high: Decimal
    or_low: Decimal
    or_close: Decimal
    or_volume: int
    atr14: Decimal
    adv14: int
    prev_close: Decimal
    gap_pct: float
    trigger_price: Decimal | None = None
    stop_price: Decimal | None = None
    max_chase_price: Decimal | None = None
    entry_limit: Decimal | None = None
    armed_at: datetime | None = None
    triggered_at: datetime | None = None
    features: dict[str, FeatureValue] = {}


class TriggerEvent(_Frozen):
    """A signal's one-shot trigger (S§3.3). ``ts`` and ``price`` are the trade's (or, for
    ``arm_check``, the snapshot's); ``received_at`` is when the engine saw it, the live trigger
    time. ``source`` ``restart`` re-serves a trigger a crash left without a decision."""

    signal_id: str
    symbol: str
    rank: int
    ts: datetime
    received_at: datetime
    price: Decimal
    size: int | None = None
    conditions: tuple[str, ...] = ()
    source: Literal["stream_trade", "arm_check", "restart"]


# ---------------------------------------------------------------- orders and the risk gate


class OrderIntent(_Frozen):
    """An entry a book wants to make: a whole-share limit buy with a protective stop (A§5).

    Values are not validated here: the risk gate judges them (R-002, R-012..R-014), so a bad
    intent is rejected with a reason instead of failing to construct. ``side`` exists so R-002
    can reject anything but a buy; exits never pass through the gate.
    """

    book: Literal["A", "B"]
    signal_id: str
    symbol: str
    side: Literal["buy", "sell"] = "buy"
    qty: int
    entry_limit: Decimal
    stop_price: Decimal
    take_profit: Decimal | None = None
    client_order_id: str
    day_size_multiplier: Decimal


class RiskCheck(_Frozen):
    """The gate's verdict. ``adjusted_qty`` is set when R-009 reduced the quantity to fit the
    settled cash; ``state_digest`` is what the rules saw, journaled in ``risk_checks``."""

    approved: bool
    violations: tuple[str, ...]
    adjusted_qty: int | None = None
    state_digest: dict[str, Any]


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
