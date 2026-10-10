from decimal import Decimal

import pytest

from trader.core.models import OrderIntent
from trader.execution.orders import (
    InvalidOrderError,
    ceil_tick,
    entry_oto_body,
    floor_tick,
    market_sell_body,
    price_text,
    stop_body,
    tick_size,
    valid_price,
)

D = Decimal


def intent(**changes: object) -> OrderIntent:
    base = OrderIntent(
        book="A",
        signal_id="20261009-F-orb5",
        symbol="F",
        qty=40,
        entry_limit=D("12.35"),
        stop_price=D("12.21"),
        client_order_id="A-20261009-F-entry-1",
        day_size_multiplier=D("1.0"),
    )
    return base.model_copy(update=changes)


# ---------------------------------------------------------------- sub-penny rules


@pytest.mark.parametrize(
    ("price", "tick"),
    [(D("1.00"), D("0.01")), (D("250.5"), D("0.01")), (D("0.99"), D("0.0001"))],
)
def test_tick_is_a_cent_from_one_dollar_up(price: Decimal, tick: Decimal) -> None:
    assert tick_size(price) == tick


@pytest.mark.parametrize(
    ("price", "ok"),
    [
        (D("12.30"), True),
        (D("12.3"), True),
        (D("12.305"), False),  # sub-penny at or above $1: Alpaca rejects with 42210000
        (D("0.1234"), True),
        (D("0.12345"), False),
        (D("1.0001"), False),
        (D("0"), False),
        (D("-1.00"), False),
    ],
)
def test_valid_price(price: Decimal, ok: bool) -> None:
    assert valid_price(price) is ok


def test_rounding_respects_direction_and_the_dollar_boundary() -> None:
    assert floor_tick(D("12.349")) == D("12.34")
    assert ceil_tick(D("12.341")) == D("12.35")
    assert ceil_tick(D("0.99995")) == D("1.00")  # crosses $1, so 2 decimals again
    assert floor_tick(D("0.99999")) == D("0.9999")
    assert str(ceil_tick(D("0.99995"))) == "1.00"


def test_price_text_is_exact() -> None:
    assert price_text(D("12.3")) == "12.30"
    assert price_text(D("0.5")) == "0.5000"
    with pytest.raises(InvalidOrderError):
        price_text(D("12.305"))


# ---------------------------------------------------------------- bodies


def test_entry_is_a_limit_oto_with_a_stop_leg_sent_as_strings() -> None:
    assert entry_oto_body(intent()) == {
        "symbol": "F",
        "qty": "40",
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": "12.35",
        "order_class": "oto",
        "stop_loss": {"stop_price": "12.21"},
        "client_order_id": "A-20261009-F-entry-1",
        "extended_hours": False,
    }


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"stop_price": D("12.35")}, "below the limit"),
        ({"stop_price": D("12.345")}, "below the limit"),  # within a cent: Alpaca rejects
        ({"stop_price": D("12.205")}, "not a valid price"),
        ({"entry_limit": D("12.351")}, "not a valid price"),
        ({"qty": 0}, "whole number"),
        ({"side": "sell"}, "long buys only"),
        ({"take_profit": D("13.00")}, "no take-profit"),
        ({"client_order_id": "A 20261009 F"}, "format"),
    ],
)
def test_entry_refuses_what_alpaca_or_the_spec_would_reject(
    changes: dict[str, object], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        entry_oto_body(intent(**changes))


def test_standalone_stop_and_market_sell() -> None:
    assert stop_body("F", 40, D("12.21"), "A-20261009-F-stop-1", "gtc") == {
        "symbol": "F",
        "qty": "40",
        "side": "sell",
        "type": "stop",
        "time_in_force": "gtc",
        "stop_price": "12.21",
        "client_order_id": "A-20261009-F-stop-1",
    }
    assert market_sell_body("F", 40, "A-20261009-F-exit-2")["type"] == "market"
    with pytest.raises(InvalidOrderError):
        market_sell_body("F", 0, "A-20261009-F-exit-3")
