from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from trader.core.clock import ET
from trader.core.models import Bar
from trader.data.indicators import (
    InsufficientHistoryError,
    adv,
    atr,
    atr_pct,
    gap_pct,
    or_range_atr,
    or_return_pct,
    or_vol_avg,
    rel_vol,
    true_range,
)

D = Decimal


def daily(rows: list[tuple[str, str, str, int]]) -> list[Bar]:
    """(high, low, close, volume) per session, oldest first."""
    start = datetime(2026, 9, 1, tzinfo=ET)
    return [
        Bar(ts=start + timedelta(days=i), open=D(c), high=D(h), low=D(lo), close=D(c), volume=v)
        for i, (h, lo, c, v) in enumerate(rows)
    ]


# 15 sessions; the true ranges of sessions 1..14 (each against the previous close), by hand:
#   1: max(10.60-9.90, |10.60-10.00|, |9.90-10.00|)  = 0.70
#   2: max(11.00-10.30, |11.00-10.40|, |10.30-10.40|) = 0.70
#   3: max(12.00-11.50, |12.00-10.90|, |11.50-10.90|) = 1.10  (gap up: the high-to-close leg)
#   4: max(11.00-10.20, |11.00-11.80|, |10.20-11.80|) = 1.60  (gap down: the low-to-close leg)
#   5..14: max(10.80-10.30, |10.80-10.50|, |10.30-10.50|) = 0.50 each, 5.00 in total
#   sum = 9.10, so ATR14 = 9.10 / 14 = 0.65
# Volumes of sessions 1..14: 13 x 1,000,000 + 1,400,000 = 14,400,000, so ADV14 = 1,028,571.43
SESSIONS = daily(
    [("10.50", "9.50", "10.00", 999)]
    + [("10.60", "9.90", "10.40", 1_000_000)]
    + [("11.00", "10.30", "10.90", 1_000_000)]
    + [("12.00", "11.50", "11.80", 1_000_000)]
    + [("11.00", "10.20", "10.50", 1_000_000)]
    + [("10.80", "10.30", "10.50", 1_000_000)] * 9
    + [("10.80", "10.30", "10.50", 1_400_000)]
)


def test_true_range_takes_the_widest_leg() -> None:
    assert true_range(D("10.60"), D("9.90"), prev_close=D("10.00")) == D("0.70")
    assert true_range(D("12.00"), D("11.50"), prev_close=D("10.90")) == D("1.10")
    assert true_range(D("11.00"), D("10.20"), prev_close=D("11.80")) == D("1.60")


def test_atr14_is_the_mean_of_14_true_ranges() -> None:
    assert len(SESSIONS) == 15
    assert atr(SESSIONS) == D("0.6500")


def test_atr_uses_only_the_last_15_sessions() -> None:
    wild = daily([("99.00", "1.00", "50.00", 5)])
    assert atr(wild + SESSIONS) == D("0.6500")


def test_atr_is_quantized_to_a_hundredth_of_a_cent() -> None:
    # 14 true ranges of 0.10 each, except one of 0.15: 1.45 / 14 = 0.103571... -> 0.1036
    rows = [("10.10", "10.00", "10.05", 1)] * 14 + [("10.15", "10.00", "10.05", 1)]
    assert atr(daily(rows)) == D("0.1036")


def test_atr_needs_15_sessions() -> None:
    with pytest.raises(InsufficientHistoryError, match="15"):
        atr(SESSIONS[1:])


def test_adv14_is_the_floored_mean_volume_of_the_last_14_sessions() -> None:
    assert adv(SESSIONS) == 1_028_571


def test_adv_needs_14_sessions() -> None:
    with pytest.raises(InsufficientHistoryError, match="14"):
        adv(SESSIONS[:13])


def test_or_vol_avg_uses_the_last_14_sessions() -> None:
    assert or_vol_avg([9_999] + [200] * 13 + [480]) == pytest.approx(220.0)  # 3,080 / 14
    with pytest.raises(InsufficientHistoryError):
        or_vol_avg([200] * 13)


def test_rel_vol() -> None:
    assert rel_vol(3_000, 1_500.0) == pytest.approx(2.0)
    assert rel_vol(0, 1_500.0) == pytest.approx(0.0)
    assert rel_vol(3_000, 0.0) is None  # no baseline volume at all: undefined, not infinite


def test_opening_range_features() -> None:
    # OR 10.00 / 10.40 / 9.90 / 10.30, ATR 0.65, previous close 9.50
    assert or_range_atr(D("10.40"), D("9.90"), D("0.65")) == pytest.approx(0.50 / 0.65)
    assert or_return_pct(D("10.00"), D("10.30")) == pytest.approx(3.0)
    assert gap_pct(D("10.00"), D("9.50")) == pytest.approx(0.50 / 9.50 * 100)
    assert atr_pct(D("0.65"), D("9.50")) == pytest.approx(0.65 / 9.50 * 100)


def test_ratios_reject_non_positive_denominators() -> None:
    with pytest.raises(ValueError, match="atr"):
        or_range_atr(D("10.40"), D("9.90"), D("0"))
    with pytest.raises(ValueError, match="prev_close"):
        gap_pct(D("10.00"), D("0"))
