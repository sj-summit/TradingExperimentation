"""Position sizing (S§3.5, A§4.3, P3.3): a pure function, property-tested."""

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from trader.strategy.sizing import binding_term, size_position, sizing

D = Decimal


def test_a_thousand_dollar_book_is_capped_by_notional() -> None:
    # S§3.5's typical case: risk budget $10, notional cap $500 binds first.
    s = sizing(D("12.74"), D("12.64"), D(1000), D(1000), D("1.0"), D("50.0"), D(1))
    assert (s.by_risk, s.by_notional, s.by_cash) == (100, 39, 78)
    assert (s.qty, s.binding) == (39, "notional")
    assert size_position(D("12.74"), D("12.64"), D(1000), D(1000), D("1.0"), D("50.0"), D(1)) == 39


def test_the_risk_budget_binds_on_a_wide_stop() -> None:
    s = sizing(D("10.00"), D("9.50"), D(1000), D(1000), D("1.0"), D("50.0"), D(1))
    assert (s.qty, s.binding) == (20, "risk")  # $10 / $0.50


def test_settled_cash_binds_after_a_first_entry() -> None:
    s = sizing(D("12.74"), D("12.64"), D(1000), D("250.00"), D("1.0"), D("50.0"), D(1))
    assert (s.qty, s.binding) == (19, "cash")


def test_the_multiplier_scales_risk_and_notional() -> None:
    half = sizing(D("12.74"), D("12.64"), D(1000), D(1000), D("1.0"), D("50.0"), D("0.5"))
    assert (half.qty, half.binding) == (19, "notional")  # $250 / 12.74


def test_zero_quantity_says_which_term_bound() -> None:
    assert binding_term(D("600"), D("590"), D(1000), D(1000), D("1.0"), D("50.0"), D(1)) == (
        "notional"
    )  # $500 can't buy one $600 share
    no_cash = sizing(D("12.74"), D("12.64"), D(1000), D("12.73"), D("1.0"), D("50.0"), D(1))
    assert (no_cash.qty, no_cash.binding) == (0, "cash")
    negative = sizing(D("12.74"), D("12.64"), D(1000), D("-5"), D("1.0"), D("50.0"), D(1))
    assert (negative.qty, negative.by_cash) == (0, 0)


def test_a_stop_at_or_above_the_limit_sizes_nothing() -> None:
    for stop in (D("12.74"), D("12.80")):
        s = sizing(D("12.74"), stop, D(1000), D(1000), D("1.0"), D("50.0"), D(1))
        assert (s.qty, s.binding) == (0, None)
    assert size_position(D("0"), D("-1"), D(1000), D(1000), D("1.0"), D("50.0"), D(1)) == 0


prices = st.decimals(min_value=D("1.00"), max_value=D("1000.00"), places=2)
gaps = st.decimals(min_value=D("0.01"), max_value=D("50.00"), places=2)
money = st.decimals(min_value=D("0.00"), max_value=D("100000.00"), places=2)
pcts = st.decimals(min_value=D("0.10"), max_value=D("100.00"), places=2)
mults = st.sampled_from([D("0.5"), D("1")])


@given(limit=prices, gap=gaps, equity=money, cash=money, risk=pcts, notional=pcts, mult=mults)
def test_never_over_a_cap_and_always_maximal(
    limit: Decimal,
    gap: Decimal,
    equity: Decimal,
    cash: Decimal,
    risk: Decimal,
    notional: Decimal,
    mult: Decimal,
) -> None:
    stop = limit - gap
    qty = size_position(limit, stop, equity, cash, risk, notional, mult)
    risk_budget = risk / 100 * equity * mult
    notional_cap = notional / 100 * equity * mult

    def fits(n: int) -> bool:
        return n * limit <= cash and n * (limit - stop) <= risk_budget and n * limit <= notional_cap

    assert qty >= 0
    assert fits(qty)
    assert not fits(qty + 1)  # maximal: one more share breaks at least one cap
