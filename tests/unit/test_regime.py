"""Regime tags (A§4.8, P3.6a; DECISIONS D-041)."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from trader.core.clock import ET
from trader.core.events import Event
from trader.core.models import Bar
from trader.memory.regime import RegimeHistoryError, calendar_tags, regime_tags, signal_features

D = Decimal
START = date(2025, 6, 2)


def spy(ranges: list[str], closes: list[str] | None = None) -> list[Bar]:
    """Daily bars, one per calendar day, each spanning ``range`` around a close of 100."""
    out = []
    for i, rng in enumerate(ranges):
        close = D(closes[i]) if closes else D(100)
        half = D(rng) / 2
        ts = datetime.combine(START + timedelta(days=i), time(0), ET)
        out.append(Bar(ts=ts, open=close, high=close + half, low=close - half, close=close,
                       volume=1))  # fmt: skip
    return out


def last_day(bars: list[Bar]) -> date:
    return bars[-1].ts.date()


def test_volatility_bucket_against_a_year_of_terciles() -> None:
    calm, stormy = ["1.0"] * 280, ["1.0"] * 266 + ["3.0"] * 14
    assert regime_tags(spy(calm), last_day(spy(calm))).vol_bucket == "mid"  # all equal
    high = regime_tags(spy(stormy), last_day(spy(stormy)))
    assert high.vol_bucket == "high"
    assert high.spy_atr_pct == pytest.approx(3.0)
    quiet = ["2.0"] * 266 + ["0.5"] * 14
    assert regime_tags(spy(quiet), last_day(spy(quiet))).vol_bucket == "low"


def test_trend_is_up_only_above_the_sma() -> None:
    flat = ["100"] * 280
    bars = spy(["1.0"] * 280, flat)
    tags = regime_tags(bars, last_day(bars))
    assert (tags.spy_close, tags.spy_sma20, tags.spy_trend) == (D(100), D(100), "down")
    bars = spy(["1.0"] * 280, [*flat[:-1], "100.01"])
    assert regime_tags(bars, last_day(bars)).spy_trend == "up"


def test_bars_after_the_day_are_ignored() -> None:
    bars = spy(["1.0"] * 280 + ["9.0"] * 5)
    d = bars[279].ts.date()
    assert regime_tags(bars, d).vol_bucket == "mid"


def test_too_little_history_is_refused() -> None:
    with pytest.raises(RegimeHistoryError, match="ATR%"):
        regime_tags(spy(["1.0"] * 60), last_day(spy(["1.0"] * 60)))
    with pytest.raises(RegimeHistoryError, match="only 10"):
        regime_tags(spy(["1.0"] * 10), last_day(spy(["1.0"] * 10)))
    bars = spy(["1.0"] * 280)
    with pytest.raises(RegimeHistoryError, match="no SPY daily bar"):
        regime_tags(bars, last_day(bars) + timedelta(days=1))


def test_calendar_and_signal_features() -> None:
    tue = date(2026, 10, 13)
    cpi = Event(
        date=date(2026, 10, 14),
        time=time(8, 30),
        name="CPI",
        impact="high",
        source="https://www.bls.gov/",
    )
    minutes = Event(
        date=tue, time=time(14, 0), name="minutes", impact="medium", source="https://www.bls.gov/"
    )
    assert calendar_tags(tue, [cpi, minutes]) == {"day_of_week": "Tue", "event_day": False}
    assert calendar_tags(date(2026, 10, 14), [cpi])["event_day"] is True
    bars = spy(["1.0"] * 280)
    tags = regime_tags(bars, last_day(bars))
    assert signal_features(tags, tue, []) == {
        "regime_spy_trend": "down", "regime_vol_bucket": "mid", "day_of_week": "Tue",
        "event_day": False,
    }  # fmt: skip
    assert signal_features(None, tue, [])["regime_spy_trend"] is None
