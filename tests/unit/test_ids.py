import re
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from trader.core.ids import MAX_CLIENT_ORDER_ID_LEN, Book, Leg, client_order_id, signal_id

D = date(2026, 10, 9)


def test_signal_id_format() -> None:
    assert signal_id(D, "XYZ") == "20261009-XYZ-orb5"


def test_client_order_id_format() -> None:
    assert client_order_id("A", D, "XYZ", "entry", 1) == "A-20261009-XYZ-entry-1"
    assert client_order_id("B", D, "XYZ", "stop", 2) == "B-20261009-XYZ-stop-2"


@pytest.mark.parametrize("symbol", ["BRK.B", "abc", "", "TOOLONG", "A B", "AB-C"])
def test_bad_symbols_rejected(symbol: str) -> None:
    with pytest.raises(ValueError, match="symbol"):
        signal_id(D, symbol)
    with pytest.raises(ValueError, match="symbol"):
        client_order_id("A", D, symbol, "entry", 1)


def test_attempt_starts_at_one() -> None:
    with pytest.raises(ValueError, match="attempt"):
        client_order_id("A", D, "XYZ", "entry", 0)


def test_unknown_book_or_leg_rejected() -> None:
    with pytest.raises(ValueError, match="book"):
        client_order_id("C", D, "XYZ", "entry", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="leg"):
        client_order_id("A", D, "XYZ", "bracket", 1)  # type: ignore[arg-type]


@given(
    book=st.sampled_from(["A", "B"]),
    day=st.dates(min_value=date(2000, 1, 1), max_value=date(2099, 12, 31)),
    symbol=st.from_regex(r"[A-Z]{1,6}", fullmatch=True),
    leg=st.sampled_from(["entry", "stop", "exit"]),
    attempt=st.integers(min_value=1, max_value=10**9),
)
def test_client_order_id_always_fits_alpaca_limits(
    book: Book, day: date, symbol: str, leg: Leg, attempt: int
) -> None:
    cid = client_order_id(book, day, symbol, leg, attempt)
    assert len(cid) <= MAX_CLIENT_ORDER_ID_LEN
    assert re.fullmatch(r"[A-Za-z0-9-]+", cid)
