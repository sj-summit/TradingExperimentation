"""Pre-market candidates (llm/candidates.py; the Phase 4 plan's decision 12; DECISIONS D-063)."""

import random
from datetime import date, datetime, time
from decimal import Decimal

import pytest

from trader.core.clock import ET, et_datetime
from trader.core.models import Trade
from trader.llm.candidates import Listing, premarket_gap, select_premarket

DAY = date(2026, 11, 3)
FLOOR = Decimal("5.00")


def listing(symbol: str, prev_close: str = "10.00") -> Listing:
    return Listing(
        symbol=symbol,
        name=f"{symbol} Corp",
        exchange="NASDAQ",
        prev_close=Decimal(prev_close),
        atr14=Decimal("0.80"),
        adv14=2_000_000,
    )


def trade(symbol: str, price: str, at: datetime | None = None) -> Trade:
    return Trade(
        symbol=symbol, ts=at or et_datetime(DAY, time(8, 55)), price=Decimal(price), size=100
    )


def pick(listings, prints, news=(), **kw):
    return select_premarket(
        listings,
        prints,
        frozenset(news),
        day=DAY,
        min_price=FLOOR,
        max_candidates=kw.pop("max_candidates", 40),
        **kw,
    )


def test_gainers_rank_by_gap_then_symbol_whatever_the_input_order() -> None:
    gaps = {"AAA": "11.00", "BBB": "12.00", "CCC": "11.00", "DDD": "10.50", "EEE": "9.00"}
    listings = [listing(s) for s in gaps]
    prints = {s: trade(s, p) for s, p in gaps.items()}
    expected = ["BBB", "AAA", "CCC", "DDD"]  # EEE fell: not a gainer, no news
    for seed in range(5):
        shuffled = listings[:]
        random.Random(seed).shuffle(shuffled)
        got = pick(shuffled, prints)
        assert [c.symbol for c in got] == expected
    first = pick(listings, prints)[0]
    assert first.picked_as == "gainer"
    assert first.gap_pct == pytest.approx(20.0)
    assert first.price == Decimal("12.00")
    assert first.listing.name == "BBB Corp"


def test_only_a_print_since_four_today_counts() -> None:
    listings = [listing("EARLY"), listing("FOUR"), listing("STALE")]
    prints = {
        "EARLY": trade("EARLY", "12.00", et_datetime(DAY, time(3, 59, 59))),
        "FOUR": trade("FOUR", "12.00", et_datetime(DAY, time(4, 0))),
        "STALE": trade("STALE", "12.00", datetime(2026, 11, 2, 19, 59, tzinfo=ET)),
    }
    assert [c.symbol for c in pick(listings, prints, news=prints)] == ["FOUR"]


def test_the_price_floor_and_split_ratios_keep_symbols_out() -> None:
    listings = [
        listing("FLOOR", "4.00"),  # 5.00 is not above the floor
        listing("ABOVE", "4.00"),
        listing("REVSPLIT", "2.00"),  # 10.00 / 2.00 = 5: a 1:5 reverse split's ratio
        listing("SPLIT", "24.00"),  # 12.00 / 24.00 = 1/2: a 2:1 split's
        listing("ALMOST", "21.80"),  # 12.00 / 21.80 is 10% off 1/2: a real fall
    ]
    prints = {
        "FLOOR": trade("FLOOR", "5.00"),
        "ABOVE": trade("ABOVE", "5.01"),
        "REVSPLIT": trade("REVSPLIT", "10.00"),
        "SPLIT": trade("SPLIT", "12.00"),
        "ALMOST": trade("ALMOST", "12.00"),
    }
    got = pick(listings, prints, news=prints)
    assert [(c.symbol, c.picked_as) for c in got] == [("ABOVE", "gainer"), ("ALMOST", "news")]


def test_news_symbols_follow_the_gainers_by_the_size_of_their_gap() -> None:
    moves = {"UP1": "11.00", "UP2": "10.20", "DOWN": "8.50", "FLAT": "10.00", "DIP": "9.90"}
    listings = [listing(s) for s in moves]
    prints = {s: trade(s, p) for s, p in moves.items()}
    got = pick(listings, prints, news={"UP2", "DOWN", "FLAT", "DIP", "NOPRINT"}, gainers=1)
    assert [(c.symbol, c.picked_as) for c in got] == [
        ("UP1", "gainer"),
        ("DOWN", "news"),  # -15%
        ("UP2", "news"),  # +2%: a gainer past the gainers' cap, picked for its news
        ("DIP", "news"),  # -1%
        ("FLAT", "news"),
    ]


def ticker(prefix: str, i: int) -> str:
    return prefix + chr(65 + i // 26) + chr(65 + i % 26)


def test_thirty_gainers_and_ten_with_news_within_the_cap() -> None:
    gainers = {ticker("G", i): f"{11 + i / 100:.2f}" for i in range(50)}
    losers = {ticker("L", i): f"{9 - i / 100:.2f}" for i in range(15)}
    moves = gainers | losers
    listings = [listing(s) for s in moves]
    prints = {s: trade(s, p) for s, p in moves.items()}
    news = set(losers)
    got = pick(listings, prints, news=news)
    assert len(got) == 40
    assert [c.picked_as for c in got] == ["gainer"] * 30 + ["news"] * 10
    assert got[0].gap_pct > got[29].gap_pct > 0
    # A smaller cap cuts the news picks first, then gainers.
    assert [c.picked_as for c in pick(listings, prints, news=news, max_candidates=33)] == (
        ["gainer"] * 30 + ["news"] * 3
    )
    assert len(pick(listings, prints, news=news, max_candidates=12)) == 12


def test_no_print_or_no_previous_close_means_no_candidate() -> None:
    listings = [listing("NOPRINT"), listing("ZERO", "0"), listing("OK")]
    prints = {"ZERO": trade("ZERO", "12.00"), "OK": trade("OK", "12.00")}
    assert [c.symbol for c in pick(listings, prints, news={"NOPRINT", "ZERO"})] == ["OK"]


def test_premarket_gap() -> None:
    prev = Decimal("400.00")
    assert premarket_gap(prev, trade("SPY", "398.00"), day=DAY) == pytest.approx(-0.5)
    assert premarket_gap(prev, None, day=DAY) is None
    assert premarket_gap(Decimal(0), trade("SPY", "398.00"), day=DAY) is None
    assert premarket_gap(prev, trade("SPY", "0"), day=DAY) is None
    late_last_night = trade("SPY", "398.00", datetime(2026, 11, 2, 19, 0, tzinfo=ET))
    assert premarket_gap(prev, late_last_night, day=DAY) is None


def test_news_picks_are_capped_at_ten_after_fewer_gainers() -> None:
    gainers = {ticker("G", i): f"{11 + i / 100:.2f}" for i in range(5)}
    fallers = {ticker("N", i): f"{9 - i / 100:.2f}" for i in range(15)}
    moves = gainers | fallers
    listings = [listing(s) for s in moves]
    prints = {s: trade(s, p) for s, p in moves.items()}
    got = pick(listings, prints, news=set(fallers))
    assert [c.picked_as for c in got] == ["gainer"] * 5 + ["news"] * 10


def test_news_picks_keep_their_own_print_and_signed_gap() -> None:
    at = et_datetime(DAY, time(8, 41))
    listings = [listing("UPP"), listing("DWN"), listing("FLT")]
    prints = {
        "UPP": trade("UPP", "10.50"),
        "DWN": trade("DWN", "9.50", at),
        "FLT": trade("FLT", "10.00"),
    }
    for order in (listings, listings[::-1]):
        got = pick(order, prints, news={"UPP", "DWN"}, gainers=0)
        assert [c.symbol for c in got] == ["DWN", "UPP"]  # +5% and -5%: the symbol decides
    dwn = pick(listings, prints, news={"DWN"})[1]
    assert (dwn.symbol, dwn.picked_as, dwn.price, dwn.printed_at) == (
        "DWN",
        "news",
        Decimal("9.50"),
        at,
    )
    assert dwn.gap_pct == pytest.approx(-5.0)
    # Flat and without news: neither a gainer nor a news pick.
    assert [c.symbol for c in pick(listings, prints)] == ["UPP"]


def test_a_print_that_cannot_set_a_price_sets_no_gap() -> None:
    odd_lot = Trade(
        symbol="ODD",
        ts=et_datetime(DAY, time(8, 55)),
        price=Decimal("12.50"),
        size=3,
        conditions=("@", "T", "I"),
    )
    assert premarket_gap(Decimal("10.00"), odd_lot, day=DAY) is None
    assert pick([listing("ODD")], {"ODD": odd_lot}, news={"ODD"}) == []
