"""Shadow-outcome simulation (S§7.2, P3.6a; DECISIONS D-041), on hand-computed paths."""

from datetime import date, datetime, time
from decimal import Decimal

import pytest

from trader.core.clock import ET
from trader.core.models import Bar
from trader.memory.shadow import ShadowParams, simulate_long, simulate_short, slippage

D = Decimal
DAY = date(2026, 10, 13)
PARAMS = ShadowParams(slippage_fixed=D("0.01"), slippage_pct=D("0.02"))
WINDOW = datetime.combine(DAY, time(9, 35), ET)
LAST = datetime.combine(DAY, time(15, 30), ET)
FLAT = datetime.combine(DAY, time(15, 55), ET)
# A long: trigger 10.00, stop 9.90. Slippage 0.01 + 0.02% of 10.00 = 0.012, so the entry is
# 10.012 and the risk 0.112 per share.
ENTRY, RISK = D("10.012"), D("0.112")


def m(hhmm: str, o: str, h: str, low: str, c: str) -> Bar:
    hh, mm = map(int, hhmm.split(":"))
    ts = datetime.combine(DAY, time(hh, mm), ET)
    return Bar(ts=ts, open=D(o), high=D(h), low=D(low), close=D(c), volume=1_000)


def long(bars: list[Bar], **kw: object) -> object:
    return simulate_long(bars, trigger=D("10.00"), stop=D("9.90"), window_start=WINDOW,
                         last_entry=LAST, flatten_at=FLAT, params=PARAMS, **kw)  # type: ignore[arg-type]  # fmt: skip


def r(exit_price: str) -> float:
    return float((D(exit_price) - ENTRY) / RISK)


def test_slippage() -> None:
    assert slippage(D("10.00"), PARAMS) == D("0.012")


def test_a_stop_in_the_trigger_minute_exits_at_the_stop_even_from_an_open_below_it() -> None:
    # The bar opened at 9.85, under the stop, then rallied through the trigger and came back.
    # S§7.2's min(open, stop) would exit at 9.84; the open was before the entry, so 9.89.
    out = simulate_long([m("09:35", "9.85", "10.05", "9.80", "9.95")], trigger=D("10.00"),
                        stop=D("9.90"), window_start=WINDOW, last_entry=LAST, flatten_at=FLAT,
                        params=PARAMS)  # fmt: skip
    assert (out.exit_reason, out.entry_price, out.exit_price) == ("stop", ENTRY, D("9.89"))
    assert out.shadow_r == pytest.approx(r("9.89"))  # -1.089
    assert out.mfe_r == pytest.approx(r("10.05"))  # the whole trigger bar counts
    assert out.mae_r == pytest.approx(r("9.80"))
    assert (out.minutes_held, out.trigger_ts) == (0, WINDOW)


def test_bars_before_0935_never_trigger() -> None:
    out = simulate_long(
        [m("09:34", "10.05", "10.20", "10.00", "10.10"),
         m("09:35", "9.95", "9.99", "9.92", "9.95")],
        trigger=D("10.00"), stop=D("9.90"), window_start=WINDOW, last_entry=LAST,
        flatten_at=FLAT, params=PARAMS,
    )  # fmt: skip
    assert (out.exit_reason, out.triggered, out.shadow_r) == ("no_trigger", False, None)


def test_a_later_stop_exits_at_the_stop_or_a_gap_below_it() -> None:
    trig = m("09:36", "9.98", "10.02", "9.95", "10.00")
    touch = [trig, m("09:37", "9.95", "9.97", "9.85", "9.88")]
    gap = [trig, m("09:37", "9.80", "9.82", "9.70", "9.75")]
    kw = {"trigger": D("10.00"), "stop": D("9.90"), "window_start": WINDOW, "last_entry": LAST,
          "flatten_at": FLAT, "params": PARAMS}  # fmt: skip
    a = simulate_long(touch, **kw)  # type: ignore[arg-type]
    b = simulate_long(gap, **kw)  # type: ignore[arg-type]
    assert (a.exit_reason, a.exit_price, a.minutes_held) == ("stop", D("9.89"), 1)
    assert (b.exit_reason, b.exit_price) == ("stop", D("9.79"))  # min(open 9.80, stop) - 0.01
    assert b.mae_r == pytest.approx(r("9.70"))


def test_a_winner_exits_at_the_flatten_minutes_open() -> None:
    bars = [
        m("09:36", "9.98", "10.02", "9.95", "10.00"),
        m("12:00", "10.30", "10.60", "10.25", "10.55"),
        m("15:54", "10.45", "10.48", "10.40", "10.44"),
        m("15:55", "10.50", "10.52", "10.30", "10.35"),  # its high/low come after the exit
    ]
    out = simulate_long(bars, trigger=D("10.00"), stop=D("9.90"), window_start=WINDOW,
                        last_entry=LAST, flatten_at=FLAT, params=PARAMS)  # fmt: skip
    assert (out.exit_reason, out.exit_price, out.exit_ts) == ("flatten", D("10.50"), FLAT)
    assert out.shadow_r == pytest.approx(r("10.50"))
    assert out.mfe_r == pytest.approx(r("10.60"))
    assert out.mae_r == pytest.approx(r("9.95"))
    assert out.minutes_held == 379  # 09:36 -> 15:55


def test_a_missing_flatten_bar_uses_the_next_open_else_the_last_close() -> None:
    kw = {"trigger": D("10.00"), "stop": D("9.90"), "window_start": WINDOW, "last_entry": LAST,
          "flatten_at": FLAT, "params": PARAMS}  # fmt: skip
    trig = m("09:36", "9.98", "10.02", "9.95", "10.00")
    later = simulate_long([trig, m("15:54", "10.45", "10.48", "10.40", "10.30"),
                           m("15:57", "10.40", "10.41", "10.39", "10.40")], **kw)  # type: ignore[arg-type]  # fmt: skip
    assert (later.exit_price, later.exit_ts.time()) == (D("10.40"), time(15, 57))  # type: ignore[union-attr]
    early = simulate_long([trig, m("15:54", "10.45", "10.48", "10.40", "10.30")], **kw)  # type: ignore[arg-type]
    assert (early.exit_reason, early.exit_price) == ("flatten", D("10.30"))


def test_no_trigger_before_last_entry() -> None:
    out = simulate_long([m("15:30", "9.99", "10.10", "9.98", "10.05")], trigger=D("10.00"),
                        stop=D("9.90"), window_start=WINDOW, last_entry=LAST, flatten_at=FLAT,
                        params=PARAMS)  # fmt: skip
    assert out.exit_reason == "no_trigger"


def test_no_bars_is_no_data() -> None:
    out = simulate_long([], trigger=D("10.00"), stop=D("9.90"), window_start=WINDOW,
                        last_entry=LAST, flatten_at=FLAT, params=PARAMS)  # fmt: skip
    assert out.exit_reason == "no_data"


def test_the_backtest_can_enter_at_a_gapped_open() -> None:
    bars = [
        m("09:36", "10.05", "10.10", "10.01", "10.08"),
        m("15:55", "10.20", "10.2", "10.2", "10.2"),
    ]
    kw = {"trigger": D("10.00"), "stop": D("9.90"), "window_start": WINDOW, "last_entry": LAST,
          "flatten_at": FLAT, "params": PARAMS}  # fmt: skip
    assert simulate_long(bars, **kw).entry_price == ENTRY  # type: ignore[arg-type]
    gapped = simulate_long(bars, entry_mode="bar_open", **kw)  # type: ignore[arg-type]
    assert (gapped.entry_price, gapped.trigger_bar_open) == (D("10.062"), D("10.05"))


def test_an_early_close_flattens_at_its_own_minute() -> None:
    early_flat = datetime.combine(DAY, time(12, 55), ET)
    bars = [
        m("09:36", "9.98", "10.02", "9.95", "10.00"),
        m("12:55", "10.25", "10.3", "10.2", "10.2"),
    ]
    out = simulate_long(bars, trigger=D("10.00"), stop=D("9.90"), window_start=WINDOW,
                        last_entry=datetime.combine(DAY, time(12, 30), ET), flatten_at=early_flat,
                        params=PARAMS)  # fmt: skip
    assert (out.exit_price, out.exit_ts) == (D("10.25"), early_flat)


# A shadow short: trigger 9.50, stop 9.60. Slippage 0.01 + 0.02% of 9.50 = 0.0119, so it sells
# at 9.4881 with a risk of 0.1119.
S_ENTRY, S_RISK = D("9.4881"), D("0.1119")


def short(bars: list[Bar]) -> object:
    return simulate_short(bars, trigger=D("9.50"), stop=D("9.60"), window_start=WINDOW,
                          last_entry=LAST, flatten_at=FLAT, params=PARAMS)  # fmt: skip


def test_a_shadow_short_mirrors_the_rules() -> None:
    win = short(
        [m("09:40", "9.52", "9.55", "9.45", "9.48"), m("15:55", "9.30", "9.31", "9.2", "9.2")]
    )
    assert (win.exit_reason, win.entry_price, win.exit_price) == ("flatten", S_ENTRY, D("9.30"))  # type: ignore[attr-defined]
    assert win.shadow_r == pytest.approx(float((S_ENTRY - D("9.30")) / S_RISK))  # type: ignore[attr-defined]
    assert win.mfe_r == pytest.approx(float((S_ENTRY - D("9.30")) / S_RISK))  # type: ignore[attr-defined]
    assert win.mae_r == pytest.approx(float((S_ENTRY - D("9.55")) / S_RISK))  # type: ignore[attr-defined]
    stopped = short([m("09:40", "9.65", "9.66", "9.45", "9.62")])  # opened above the stop
    assert (stopped.exit_reason, stopped.exit_price) == ("stop", D("9.61"))  # type: ignore[attr-defined]
    later = short(
        [m("09:40", "9.52", "9.55", "9.45", "9.48"), m("09:41", "9.70", "9.75", "9.6", "9.7")]
    )
    assert later.exit_price == D("9.71")  # type: ignore[attr-defined]  # max(open 9.70, stop) + 0.01
