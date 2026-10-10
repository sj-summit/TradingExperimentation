from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.nyse import calendar_rows
from trader.config import FeesConfig
from trader.core.calendar import TradingCalendar, parse_alpaca_calendar
from trader.core.clock import ET, SimClock
from trader.core.models import OrderIntent, RiskCheck
from trader.execution.broker import Order
from trader.execution.records import Records
from trader.memory.journal import Journal
from trader.risk.ledger import read_entries

D = Decimal
T0 = datetime(2026, 10, 9, 9, 45, tzinfo=ET)
FIRST, LAST = date(2026, 9, 1), date(2026, 12, 31)
CAL = TradingCalendar(parse_alpaca_calendar(calendar_rows(FIRST, LAST)), FIRST, LAST)
FEES = FeesConfig(
    sec_fee_per_million_sold=D("20.60"),
    finra_taf_per_share_sold=D("0.000195"),
    finra_taf_max_per_trade=D("9.79"),
)


def order(**changes: Any) -> Order:
    base = Order(
        id="o1",
        client_order_id="A-20261009-XYZ-entry-1",
        symbol="XYZ",
        side="buy",
        type="limit",
        order_class="oto",
        time_in_force="day",
        qty=40,
        filled_qty=0,
        limit_price=D("12.35"),
        status="new",
        submitted_at=T0,
        updated_at=T0,
        group_id="g1",
        group_role="entry",
    )
    return base.model_copy(update=changes)


def leg(**changes: Any) -> Order:
    base = order(
        id="leg1",
        client_order_id="alpaca-generated",
        side="sell",
        type="stop",
        limit_price=None,
        stop_price=D("12.21"),
        status="held",
        group_role="stop_loss",
    )
    return base.model_copy(update=changes)


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    yield opened
    await opened.aclose()


@pytest.fixture
def clock() -> SimClock:
    return SimClock(T0)


@pytest.fixture
def records(journal: Journal, clock: SimClock) -> Records:
    return Records(journal, book="A", calendar=CAL, fees=FEES, clock=clock)


async def fills(journal: Journal) -> list[tuple[str, int, str]]:
    rows = await journal.fetch_all("SELECT id, qty, price FROM fills ORDER BY filled_at, id")
    return [(r["id"], r["qty"], r["price"]) for r in rows]


async def ledger(journal: Journal) -> list[tuple[str, Decimal, str | None]]:
    entries = await journal.write(lambda conn: read_entries(conn, "A"))
    return [(e.kind, e.amount, e.ref) for e in entries]


# ---------------------------------------------------------------- fills are booked once


async def test_a_stream_fill_then_the_rest_catch_up_books_once(
    records: Records, journal: Journal
) -> None:
    partial = order(status="partially_filled", filled_qty=15, filled_avg_price=D("12.34"))
    booked = await records.apply(partial, execution_price=D("12.34"), execution_qty=15)
    assert [(f.fill_id, f.qty, f.price) for f in booked] == [("o1:15", 15, D("12.34"))]
    assert await records.apply(partial) == []  # the REST snapshot of the same state
    assert await fills(journal) == [("o1:15", 15, "12.34")]
    assert await ledger(journal) == [("buy", D("-185.10"), "fill:o1:15")]


async def test_a_rest_catch_up_then_late_stream_events_book_once(
    records: Records, journal: Journal
) -> None:
    # Two executions, 15 @ 12.34 and 25 @ 12.33, seen first as one REST snapshot.
    done = order(status="filled", filled_qty=40, filled_avg_price=D("12.33375"))
    assert [f.qty for f in await records.apply(done)] == [40]
    late_first = order(status="partially_filled", filled_qty=15, filled_avg_price=D("12.34"))
    assert await records.apply(late_first, execution_price=D("12.34"), execution_qty=15) == []
    assert await records.apply(done, execution_price=D("12.33"), execution_qty=25) == []
    assert await fills(journal) == [("o1:40", 40, "12.33375")]


async def test_a_catch_up_books_only_the_missing_part_at_its_own_price(
    records: Records, journal: Journal
) -> None:
    partial = order(status="partially_filled", filled_qty=15, filled_avg_price=D("12.34"))
    await records.apply(partial, execution_price=D("12.34"), execution_qty=15)
    done = order(status="filled", filled_qty=40, filled_avg_price=D("12.33375"))
    (rest,) = await records.apply(done)  # the stream missed the second execution
    assert (rest.fill_id, rest.qty, rest.price) == ("o1:40", 25, D("12.33"))
    assert sum(amount for _, amount, _ in await ledger(journal)) == D("-493.35")


async def test_a_sale_books_proceeds_and_its_fee_estimate(
    records: Records, journal: Journal
) -> None:
    stop = leg(status="filled", filled_qty=40, filled_avg_price=D("12.20"))
    await records.apply(stop, execution_price=D("12.20"), execution_qty=40)
    assert await ledger(journal) == [
        ("sell", D("488.00"), "fill:leg1:40"),
        ("fee", D("-0.02"), "fee-est:leg1:40"),
    ]


async def test_a_fill_without_a_price_waits_for_one(records: Records, journal: Journal) -> None:
    assert await records.apply(order(status="partially_filled", filled_qty=15)) == []
    assert await fills(journal) == []


# ---------------------------------------------------------------- order rows


async def test_pending_entry_then_the_brokers_answer(records: Records, journal: Journal) -> None:
    intent = OrderIntent(
        book="A",
        signal_id="20261009-XYZ-orb5",
        symbol="XYZ",
        qty=40,
        entry_limit=D("12.35"),
        stop_price=D("12.21"),
        client_order_id="A-20261009-XYZ-entry-1",
        day_size_multiplier=D("1"),
    )
    check = RiskCheck(approved=True, violations=(), adjusted_qty=30, state_digest={})
    await records.pending_entry(intent, check)
    row = await records.order("A-20261009-XYZ-entry-1")
    assert row is not None
    assert (row.status, row.qty, row.broker_order_id) == ("submitting", 30, None)  # R-009 cut
    assert await journal.fetch_one("SELECT approved FROM risk_checks") is not None
    await records.apply(order(qty=30, legs=(leg(qty=30),)))
    row = await records.order("A-20261009-XYZ-entry-1")
    assert row is not None
    assert (row.status, row.broker_order_id, row.group_id) == ("new", "o1", "g1")
    stop_leg = await records.order("alpaca-generated")
    assert stop_leg is not None
    assert (stop_leg.parent_client_order_id, stop_leg.signal_id) == (
        "A-20261009-XYZ-entry-1",
        "20261009-XYZ-orb5",
    )
    assert await records.planned_stop("XYZ") == D("12.21")


async def test_a_streamed_leg_finds_its_entry_by_group(records: Records) -> None:
    await records.apply(order(), signal_id="20261009-XYZ-orb5")
    await records.apply(leg(status="new"))  # the stream never nests legs
    stop_leg = await records.order("alpaca-generated")
    assert stop_leg is not None
    assert (stop_leg.parent_client_order_id, stop_leg.signal_id) == (
        "A-20261009-XYZ-entry-1",
        "20261009-XYZ-orb5",
    )


async def test_a_final_status_never_goes_back(records: Records) -> None:
    await records.apply(order(status="canceled", updated_at=T0 + timedelta(seconds=10)))
    await records.apply(order(status="partially_filled", updated_at=T0 + timedelta(seconds=20)))
    row = await records.order("A-20261009-XYZ-entry-1")
    assert row is not None
    assert row.status == "canceled"


async def test_an_older_snapshot_never_replaces_a_newer_one(records: Records) -> None:
    await records.apply(order(status="partially_filled", updated_at=T0 + timedelta(seconds=5)))
    await records.apply(order(status="new", updated_at=T0 + timedelta(seconds=1)))
    row = await records.order("A-20261009-XYZ-entry-1")
    assert row is not None
    assert row.status == "partially_filled"


async def test_exit_and_stop_ids_take_the_next_attempt(records: Records) -> None:
    first = await records.pending_sell(symbol="XYZ", qty=40, leg="exit")
    second = await records.pending_sell(symbol="XYZ", qty=40, leg="exit")
    stop = await records.pending_sell(symbol="XYZ", qty=40, leg="stop", stop_price=D("12.21"))
    assert (first, second, stop) == (
        "A-20261009-XYZ-exit-1",
        "A-20261009-XYZ-exit-2",
        "A-20261009-XYZ-stop-1",
    )


async def test_mark_settles_only_unconfirmed_orders(records: Records) -> None:
    cid = await records.pending_sell(symbol="XYZ", qty=40, leg="exit")
    await records.mark(cid, "uncertain")
    await records.mark(cid, "lost")
    row = await records.order(cid)
    assert row is not None
    assert row.status == "lost"
    await records.apply(order(status="filled", filled_qty=40, filled_avg_price=D("12.33")))
    await records.mark("A-20261009-XYZ-entry-1", "lost")  # the broker's word stands
    entry = await records.order("A-20261009-XYZ-entry-1")
    assert entry is not None
    assert entry.status == "filled"
