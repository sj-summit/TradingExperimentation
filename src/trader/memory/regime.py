"""Regime tags (A§4.8; DECISIONS D-041): pre-decision features for the next session's signals.

For a session ``d``, from SPY's SIP daily bars through ``d``:

- ``spy_trend``: ``up`` when SPY closed above its 20-session SMA, else ``down``;
- ``vol_bucket``: SPY's ATR% (D-025's ATR14 / close x 100) against the terciles of the same
  measure over the past year (252 sessions): ``low`` below the lower tercile, ``high`` above
  the upper one, else ``mid``.

They are computed at boot for the previous session, whose bars are complete by then, and signals
on day t carry t-1's tags (A§8's "prior-day regime"). ``day_of_week`` and ``event_day`` are known
before the open, so they describe the trading day itself.
"""

import sqlite3
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal, Protocol

import structlog

from trader.core.clock import ET, Clock, fmt_iso
from trader.core.events import Event
from trader.core.models import Bar, FeatureValue
from trader.data import indicators
from trader.data.alpaca_rest import Adjustment, AlpacaError, Feed, Timeframe
from trader.memory.journal import Journal, dec_to_db

log = structlog.get_logger(__name__)

SMA_SESSIONS = 20
YEAR_SESSIONS = 252
MIN_TERCILE_SESSIONS = 60  # fewer ATR% values than this can't place terciles


class RegimeHistoryError(ValueError):
    """Not enough SPY daily bars to compute the tags."""


@dataclass(frozen=True)
class RegimeTags:
    date: date  # the session whose close the tags describe
    spy_close: Decimal
    spy_sma20: Decimal
    spy_trend: Literal["up", "down"]
    spy_atr_pct: float
    tercile_low: float
    tercile_high: float
    vol_bucket: Literal["low", "mid", "high"]


def regime_tags(daily: Sequence[Bar], d: date) -> RegimeTags:
    """SPY's tags as of session ``d``'s close; bars after ``d`` are ignored."""
    bars = sorted((b for b in daily if b.ts.date() <= d), key=lambda b: b.ts)
    if not bars or bars[-1].ts.date() != d:
        raise RegimeHistoryError(f"no SPY daily bar for {d}")
    if len(bars) < max(SMA_SESSIONS, indicators.PERIOD + 1):
        raise RegimeHistoryError(f"only {len(bars)} SPY daily bars up to {d}")
    close = bars[-1].close
    sma = sum((b.close for b in bars[-SMA_SESSIONS:]), Decimal(0)) / SMA_SESSIONS
    series = [
        _atr_pct(bars[: end + 1])
        for end in range(max(indicators.PERIOD, len(bars) - YEAR_SESSIONS), len(bars))
    ]
    if len(series) < MIN_TERCILE_SESSIONS:
        raise RegimeHistoryError(f"only {len(series)} sessions of ATR% before {d}")
    # quantiles() interpolates in floats (a flat series gives 0.8000000000000002 for 0.8), so
    # values are compared at 9 decimals: a day on a tercile must not change bucket by rounding.
    low, high = (round(q, 9) for q in statistics.quantiles(series, n=3))
    today = round(series[-1], 9)
    return RegimeTags(
        date=d,
        spy_close=close,
        spy_sma20=sma.quantize(Decimal("0.0001")),
        spy_trend="up" if close > sma else "down",
        spy_atr_pct=today,
        tercile_low=low,
        tercile_high=high,
        vol_bucket="low" if today < low else "high" if today > high else "mid",
    )


def history_start(d: date) -> datetime:
    """Far enough back for a year of ATR% values (the first query for ``regime_tags``)."""
    return datetime.combine(d - timedelta(days=400), time(0), ET)


def calendar_tags(d: date, events: Sequence[Event]) -> dict[str, FeatureValue]:
    """The trading day's own tags, known before the open."""
    return {
        "day_of_week": d.strftime("%a"),
        "event_day": any(e.date == d and e.impact == "high" for e in events),
    }


def signal_features(
    tags: RegimeTags | None, d: date, events: Sequence[Event]
) -> dict[str, FeatureValue]:
    """The DSL's regime and calendar fields for signals on day ``d`` (S§6.2)."""
    features: dict[str, FeatureValue] = {
        "regime_spy_trend": tags.spy_trend if tags else None,
        "regime_vol_bucket": tags.vol_bucket if tags else None,
    }
    return features | calendar_tags(d, events)


def _atr_pct(bars: Sequence[Bar]) -> float:
    return indicators.atr_pct(indicators.atr(bars), bars[-1].close)


# ---------------------------------------------------------------- the boot job (P3.6b)


class RegimeMarket(Protocol):
    async def bars(
        self,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
        *,
        feed: Feed,
        adjustment: Adjustment = "raw",
    ) -> dict[str, list[Bar]]: ...


async def load_regime(journal: Journal, d: date) -> RegimeTags | None:
    row = await journal.fetch_one("SELECT * FROM regime_daily WHERE date = ?", (d.isoformat(),))
    if row is None:
        return None
    return RegimeTags(
        date=d,
        spy_close=Decimal(row["spy_close"]),
        spy_sma20=Decimal(row["spy_sma20"]),
        spy_trend=row["spy_trend"],
        spy_atr_pct=float(row["spy_atr_pct"]),
        tercile_low=float(row["tercile_low"]),
        tercile_high=float(row["tercile_high"]),
        vol_bucket=row["vol_bucket"],
    )


async def ensure_regime(
    *, journal: Journal, market: RegimeMarket, clock: Clock, d: date, symbol: str = "SPY"
) -> RegimeTags | None:
    """Session ``d``'s tags, stored once in ``regime_daily``. Run at boot for the previous
    session, whose bars are complete by then. Returns None (and logs) when they can't be
    computed: signals then carry no regime tags rather than wrong ones."""
    stored = await load_regime(journal, d)
    if stored is not None:
        return stored
    try:
        found = await market.bars(
            [symbol], "1Day", history_start(d), feed="sip", adjustment="split"
        )
        tags = regime_tags(found.get(symbol, []), d)
    except (AlpacaError, RegimeHistoryError) as exc:
        log.warning("regime unavailable", day=d.isoformat(), error=str(exc))
        return None
    now = clock.now()
    await journal.write(lambda conn: store_regime(conn, tags, now))
    return tags


def store_regime(conn: sqlite3.Connection, tags: RegimeTags, now: datetime) -> None:
    """Write one session's tags inside the caller's transaction (also seeds a replay)."""
    conn.execute(
        "INSERT OR REPLACE INTO regime_daily (date, spy_close, spy_sma20, spy_trend, spy_atr_pct, "
        "tercile_low, tercile_high, vol_bucket, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tags.date.isoformat(),
            dec_to_db(tags.spy_close),
            dec_to_db(tags.spy_sma20),
            tags.spy_trend,
            tags.spy_atr_pct,
            tags.tercile_low,
            tags.tercile_high,
            tags.vol_bucket,
            fmt_iso(now),
        ),
    )
