import sqlite3
from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fixtures.nyse import calendar_rows
from trader.config import FeesConfig
from trader.core.calendar import TradingCalendar, parse_alpaca_calendar
from trader.core.clock import ET, SimClock
from trader.memory.journal import Journal
from trader.risk.ledger import (
    LedgerEntry,
    LedgerError,
    LedgerNotSeededError,
    SettlementLedger,
    available,
    cash,
    eod_check,
    estimate_sell_fees,
    fill_entries,
    insert_entries,
    settled_cash_sod,
    start_of_day_cash,
    todays_buys,
)

D = Decimal
FIRST, LAST = date(2026, 9, 1), date(2026, 12, 31)
CAL = TradingCalendar(parse_alpaca_calendar(calendar_rows(FIRST, LAST)), FIRST, LAST)
FEES = FeesConfig(
    sec_fee_per_million_sold=D("20.60"),
    finra_taf_per_share_sold=D("0.000195"),
    finra_taf_max_per_trade=D("9.79"),
)
_refs = iter(range(1, 1_000_000))


def deposit(day: date, amount: str = "1000") -> LedgerEntry:
    return LedgerEntry(
        book="A", kind="deposit", amount=D(amount), trade_date=day, settle_date=day, ref="seed"
    )


def buy(day: date, qty: int, price: str) -> list[LedgerEntry]:
    return _fill("buy", day, qty, price)


def sell(day: date, qty: int, price: str) -> list[LedgerEntry]:
    return _fill("sell", day, qty, price)


def _fill(side: str, day: date, qty: int, price: str) -> list[LedgerEntry]:
    return fill_entries(
        book="A",
        side=side,  # type: ignore[arg-type]
        symbol="XYZ",
        qty=qty,
        price=D(price),
        trade_date=day,
        settle_date=CAL.settlement_date(day),
        fill_id=f"f{next(_refs)}",
        fees=FEES,
    )


# ---------------------------------------------------------------- fees (D-019)


def test_sell_fees_are_sec_plus_taf_rounded_up_to_the_cent() -> None:
    # $490 sold: SEC 490 * 20.60 / 1e6 = 0.010094; TAF 50 * 0.000195 = 0.00975 -> 0.019844
    assert estimate_sell_fees(50, D("9.80"), FEES) == D("0.02")
    # 60,000 shares: TAF is capped at $9.79 per trade; SEC on $600,000 = 12.36
    assert estimate_sell_fees(60_000, D("10.00"), FEES) == D("22.15")


def test_a_sale_books_proceeds_and_a_fee_estimate_settling_together() -> None:
    proceeds, fee = sell(date(2026, 10, 9), 50, "9.80")
    assert (proceeds.kind, proceeds.amount, proceeds.settle_date) == (
        "sell",
        D("490.00"),
        date(2026, 10, 13),
    )
    assert (fee.kind, fee.amount, fee.settle_date, fee.is_fee_estimate) == (
        "fee",
        D("-0.02"),
        date(2026, 10, 13),
        True,
    )
    (cost,) = buy(date(2026, 10, 9), 50, "10.00")
    assert (cost.kind, cost.amount) == ("buy", D("-500.00"))


# ---------------------------------------------------------------- RISK.md §4


def test_risk_md_example_holds_exactly_with_fees() -> None:
    """$1,000 settled: buy A $500, stopped out at $490, buy B $500, a third buy is refused."""
    day = date(2026, 10, 7)
    entries = [deposit(date(2026, 10, 6))]
    entries += buy(day, 50, "10.00")
    entries += sell(day, 50, "9.80")  # unsettled until tomorrow
    assert available(entries, day) == D(500)  # A's proceeds can't fund B
    entries += buy(day, 25, "20.00")
    assert available(entries, day) == D(0)  # buys today == settled cash at the open
    assert todays_buys(entries, day) == settled_cash_sod(entries, day) == D(1000)


def test_columbus_day_holds_friday_proceeds_until_tuesday() -> None:
    friday, monday, tuesday = date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13)
    entries = [deposit(date(2026, 10, 8))]
    entries += buy(friday, 50, "10.00")
    entries += sell(friday, 50, "9.80")
    assert CAL.settlement_date(friday) == tuesday  # banks are shut on Columbus Day
    assert available(entries, monday) == D(500)  # Friday's buy is paid; its sale isn't in
    assert available(entries, tuesday) == D("989.98")


def test_weekend_and_thanksgiving() -> None:
    entries = [deposit(date(2026, 10, 15))]
    entries += buy(date(2026, 10, 16), 10, "10.00")
    entries += sell(date(2026, 10, 16), 10, "11.00")  # Friday: settles Monday
    assert available(entries, date(2026, 10, 19)) == D("1010.00") - D("0.01")
    entries += buy(date(2026, 11, 25), 10, "10.00")  # Wednesday before Thanksgiving
    entries += sell(date(2026, 11, 25), 10, "10.00")
    assert CAL.settlement_date(date(2026, 11, 25)) == date(2026, 11, 27)
    assert available(entries, date(2026, 11, 27)) == D("1009.99") - D("0.01")


def test_a_partly_filled_entry_is_reserved_not_counted_twice() -> None:
    day = date(2026, 10, 7)
    entries = [deposit(date(2026, 10, 6))]
    entries += buy(day, 15, "12.33")  # 15 of 40 filled
    unfilled = (40 - 15) * D("12.35")  # the rest is still open at its limit
    assert available(entries, day, pending=unfilled) == D(1000) - D("184.95") - D("308.75")


def test_start_of_day_cash_is_everything_traded_before_today() -> None:
    entries = [deposit(date(2026, 10, 6))]
    entries += buy(date(2026, 10, 7), 50, "10.00")
    entries += sell(date(2026, 10, 7), 50, "10.40")
    assert start_of_day_cash(entries, date(2026, 10, 7)) == D(1000)
    # $520 sold: SEC 0.010712 + TAF 0.00975 = 0.020462, rounded up to 0.03
    assert start_of_day_cash(entries, date(2026, 10, 8)) == D("1019.97")


# ---------------------------------------------------------------- EOD cash check


def test_the_eod_check_leaves_out_only_todays_fee_estimates() -> None:
    """The broker charges a day's fees after its close (D-055), so at EOD today's are not in
    its cash yet; earlier days' are, as estimates until the actual fees are booked."""
    yesterday, day = date(2026, 10, 6), date(2026, 10, 7)
    entries = [deposit(date(2026, 10, 5)), *buy(yesterday, 50, "10.00")]
    entries += [*sell(yesterday, 50, "10.40"), *buy(day, 50, "10.00"), *sell(day, 50, "9.80")]
    # Yesterday's $520 sale was estimated at 0.03, today's $490 at 0.02 (SEC + TAF, rounded up).
    broker_cash = D(1000) - D(500) + D(520) - D("0.03") - D(500) + D(490)
    check = eod_check(entries, broker_cash, day=day, tolerance=D("1.00"))
    assert (check.ok, check.ledger_cash, check.difference) == (True, D("1009.97"), D(0))
    assert cash(entries) == D("1009.95")
    assert cash(entries, unposted_from=day) == D("1009.97")


def test_a_gap_over_the_tolerance_fails() -> None:
    entries = [deposit(date(2026, 10, 6))]
    check = eod_check(entries, D("998.99"), day=date(2026, 10, 7), tolerance=D("1.00"))
    assert not check.ok
    assert check.difference == D("-1.01")


# ---------------------------------------------------------------- property (R§4)


DAYS = [s.date for s in CAL.sessions_between(date(2026, 10, 1), date(2026, 11, 30))]


@st.composite
def trading_days(draw: st.DrawFn) -> list[list[tuple[str, int, int]]]:
    """Per trading day, a list of (action, cents, percent) attempts."""
    out = []
    for _ in DAYS:
        actions = draw(
            st.lists(
                st.tuples(
                    st.sampled_from(["buy", "sell"]),
                    st.integers(min_value=100, max_value=70_000),  # $1 .. $700
                    st.integers(min_value=70, max_value=130),  # sale at 70%..130% of cost
                ),
                max_size=4,
            )
        )
        out.append(actions)
    return out


@settings(max_examples=150, deadline=None)
@given(trading_days())
def test_buys_never_exceed_settled_cash_across_holidays(
    plan: list[list[tuple[str, int, int]]],
) -> None:
    entries = [deposit(DAYS[0] - (DAYS[1] - DAYS[0]))]
    lots: list[tuple[int, Decimal]] = []  # open positions: (qty, price)
    for day, actions in zip(DAYS, plan, strict=True):
        sod = settled_cash_sod(entries, day)
        for action, cents, percent in actions:
            if action == "buy":
                price = D(cents) / 100
                if price > available(entries, day):  # the gate (R-009) refuses the rest
                    continue
                entries += buy(day, 1, str(price))
                lots.append((1, price))
            elif lots:
                qty, price = lots.pop(0)
                entries += sell(day, qty, str((price * percent / 100).quantize(D("0.01"))))
            assert available(entries, day) >= 0
        assert todays_buys(entries, day) <= sod


# ---------------------------------------------------------------- the journal-backed ledger


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    yield opened
    await opened.aclose()


def ledger(journal: Journal) -> SettlementLedger:
    return SettlementLedger(journal, "A", SimClock(datetime(2026, 10, 9, 8, 30, tzinfo=ET)))


async def test_an_unseeded_ledger_refuses_to_answer(journal: Journal) -> None:
    book = ledger(journal)
    with pytest.raises(LedgerNotSeededError):
        await book.available()
    with pytest.raises(LedgerNotSeededError):
        await book.start_of_day_equity()


async def test_seed_only_from_a_flat_account_and_only_once(journal: Journal) -> None:
    book = ledger(journal)
    with pytest.raises(LedgerError, match="flat"):
        await book.seed(D("1000"), positions=1, open_orders=0)
    with pytest.raises(LedgerError, match="flat"):
        await book.seed(D("1000"), positions=0, open_orders=2)
    await book.seed(D("1000.00"), positions=0, open_orders=0)
    assert await book.available() == D("1000.00")
    assert await book.start_of_day_equity() == D("1000.00")  # a deposit counts from its own day
    with pytest.raises(LedgerError, match="already seeded"):
        await book.seed(D("1000.00"), positions=0, open_orders=0)


async def test_rows_round_trip_and_a_fill_can_be_booked_only_once(journal: Journal) -> None:
    book = ledger(journal)
    await book.seed(D("1000"), positions=0, open_orders=0)
    rows = sell(date(2026, 10, 9), 50, "9.80")
    now = datetime(2026, 10, 9, 10, 0, tzinfo=ET)
    await journal.write(lambda conn: insert_entries(conn, rows, now))
    stored = await book.entries()
    assert stored[1:] == rows
    with pytest.raises(sqlite3.IntegrityError):
        await journal.write(lambda conn: insert_entries(conn, rows[:1], now))
    assert await book.available(pending=D(100)) == D(900)  # proceeds settle Tuesday
