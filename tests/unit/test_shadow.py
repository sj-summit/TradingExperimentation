"""Shadow-outcome simulation (S§7.2, P3.6a; DECISIONS D-041), on hand-computed paths."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from trader.core.clock import ET
from trader.core.models import Bar, SipTrade
from trader.memory.shadow import (
    ShadowParams,
    ambiguous,
    resolve_trigger_minute,
    simulate_long,
    simulate_short,
    slippage,
)

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
    assert out.trigger_resolution == "fallback"  # ambiguous, and no tape was read (D-057)
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


def test_a_gap_through_enters_at_the_open_and_a_touch_stops_it() -> None:
    """D-057 (D-054's CRWG): a minute that opened past the trigger filled at its open."""
    flat = m("15:55", "10.20", "10.2", "10.2", "10.2")
    gapped = long([m("09:36", "10.05", "10.10", "10.01", "10.08"), flat])
    assert (gapped.entry_price, gapped.trigger_bar_open) == (D("10.062"), D("10.05"))  # type: ignore[attr-defined]
    assert gapped.trigger_resolution == "bars"  # type: ignore[attr-defined]
    # The open came first, so a touch of the stop in that minute came after the entry.
    touched = long([m("09:36", "10.05", "10.10", "9.85", "9.95"), flat])
    assert (touched.exit_reason, touched.exit_price) == ("stop", D("9.89"))  # type: ignore[attr-defined]
    assert touched.trigger_resolution == "bars"  # type: ignore[attr-defined]


# ---------------------------------------------------------------- the trigger minute's tape


def p(ss: float, price: str, *, cond: tuple[str, ...] = ("@",), u: str | None = None) -> SipTrade:
    ts = datetime.combine(DAY, time(9, 40), ET) + timedelta(seconds=ss)
    return SipTrade(symbol="XYZ", ts=ts, price=D(price), size=100, conditions=cond, update=u)


AMBIGUOUS = m("09:40", "9.95", "10.05", "9.85", "9.97")  # opened short, touched, closed safe
LATER = [m("09:41", "10.00", "10.10", "9.95", "10.05"), m("15:55", "10.30", "10.3", "10.3", "10.3")]


def test_which_trigger_minutes_bars_cant_order() -> None:
    def it(bar: Bar) -> bool:
        return ambiguous(bar, sign=1, trigger=D("10.00"), stop=D("9.90"))

    assert it(AMBIGUOUS)
    assert not it(m("09:40", "10.00", "10.05", "9.85", "9.97"))  # opened at the trigger
    assert not it(m("09:40", "9.95", "10.05", "9.85", "9.90"))  # closed at the stop
    assert not it(m("09:40", "9.95", "10.05", "9.91", "9.97"))  # never touched the stop

    def short_it(bar: Bar) -> bool:  # trigger 9.50, stop 9.60
        return ambiguous(bar, sign=-1, trigger=D("9.50"), stop=D("9.60"))

    assert short_it(m("09:40", "9.55", "9.65", "9.45", "9.52"))
    assert not short_it(m("09:40", "9.50", "9.65", "9.45", "9.52"))  # opened at the trigger
    assert not short_it(m("09:40", "9.55", "9.65", "9.45", "9.60"))  # closed at the stop
    assert not short_it(m("09:40", "9.55", "9.59", "9.45", "9.52"))  # never touched the stop


def test_an_ambiguous_minute_entered_then_stopped() -> None:
    tape = [p(1, "9.95"), p(10, "10.01"), p(30, "9.88"), p(55, "10.05")]
    out = long([AMBIGUOUS, *LATER], tape=tape)
    assert (out.exit_reason, out.exit_price) == ("stop", D("9.87"))  # type: ignore[attr-defined]  # min(9.90, 9.88) - 0.01
    assert out.trigger_resolution == "tape"  # type: ignore[attr-defined]
    # Only the prints it held count: 10.01 at best, not the bar's high nor 10.05 after the exit.
    assert out.mfe_r == pytest.approx(r("10.01"))  # type: ignore[attr-defined]
    assert out.mae_r == pytest.approx(r("9.87"))  # type: ignore[attr-defined]  # the exit


def test_an_ambiguous_minute_whose_stop_came_first_is_not_stopped() -> None:
    tape = [p(1, "9.95"), p(10, "9.88"), p(30, "10.02"), p(50, "9.97")]
    out = long([AMBIGUOUS, *LATER], tape=tape)
    assert (out.exit_reason, out.exit_price) == ("flatten", D("10.30"))  # type: ignore[attr-defined]
    assert out.trigger_resolution == "tape"  # type: ignore[attr-defined]
    assert out.entry_price == ENTRY  # type: ignore[attr-defined]  # it opened below: at the trigger
    assert out.tape_entry == D("10.02")  # type: ignore[attr-defined]  # the first print past it
    # The touch at 9.88 came before the entry: the worst it saw held was 09:41's 9.95.
    assert out.mae_r == pytest.approx(r("9.95"))  # type: ignore[attr-defined]
    assert out.mfe_r == pytest.approx(r("10.30"))  # type: ignore[attr-defined]  # the exit


def test_void_and_odd_lot_prints_never_order_the_minute() -> None:
    tape = [
        p(1, "10.01", u="canceled"),  # never happened as printed
        p(2, "10.02", cond=("@", "I")),  # an odd lot sets no price (D-043)
        p(10, "9.88"),  # so the stop came first...
        p(30, "10.01", u="corrected"),  # ...and this valid correction is the entry
        p(50, "9.97"),
    ]
    out = long([AMBIGUOUS, *LATER], tape=tape)
    assert (out.exit_reason, out.trigger_resolution) == ("flatten", "tape")  # type: ignore[attr-defined]
    voided = long([AMBIGUOUS, *LATER], tape=[*tape, p(55, "9.89", u="incorrect")])
    assert voided.exit_reason == "flatten"  # type: ignore[attr-defined]  # a void print can't stop it


def test_a_tape_with_no_print_at_the_trigger_falls_back() -> None:
    out = long([AMBIGUOUS, *LATER], tape=[p(1, "9.95"), p(10, "9.88"), p(50, "9.97")])
    assert (out.exit_reason, out.exit_price) == ("stop", D("9.89"))  # type: ignore[attr-defined]
    assert out.trigger_resolution == "fallback"  # type: ignore[attr-defined]
    assert resolve_trigger_minute([], sign=1, trigger=D("10.00"), stop=D("9.90")) is None
    outside = [p(-5, "10.01"), p(60, "10.01")]  # the minutes either side, 09:41:00.000 included
    assert long([AMBIGUOUS, *LATER], tape=outside).trigger_resolution == "fallback"  # type: ignore[attr-defined]


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


def test_a_shadow_shorts_trigger_minute_mirrors_the_tape_rule() -> None:
    bar = m("09:40", "9.55", "9.65", "9.45", "9.52")  # opened above, touched 9.60, closed below
    rest = [m("15:55", "9.30", "9.31", "9.2", "9.2")]

    def run(bars: list[Bar], tape: list[SipTrade] | None = None) -> object:
        return simulate_short(
            bars,
            trigger=D("9.50"),
            stop=D("9.60"),
            window_start=WINDOW,
            last_entry=LAST,
            flatten_at=FLAT,
            params=PARAMS,
            tape=tape,
        )

    entered_first = [p(1, "9.55"), p(10, "9.49"), p(30, "9.62"), p(50, "9.52")]
    out = run([bar, *rest], entered_first)
    assert (out.exit_reason, out.exit_price) == ("stop", D("9.63"))  # type: ignore[attr-defined]
    assert out.trigger_resolution == "tape"  # type: ignore[attr-defined]
    stop_first = [p(1, "9.55"), p(10, "9.62"), p(30, "9.49"), p(50, "9.52")]
    assert (run([bar, *rest], stop_first).exit_reason) == "flatten"  # type: ignore[attr-defined]
    gap = run([m("09:40", "9.45", "9.48", "9.40", "9.42"), *rest])
    assert gap.entry_price == D("9.45") - D("0.0119")  # type: ignore[attr-defined]  # opened below
