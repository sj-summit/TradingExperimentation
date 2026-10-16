"""The one flatten routine (D-036) against SimBroker, with the journal underneath."""

from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.fixtures.nyse import calendar_rows
from tests.fixtures.simtime import run_on_clock
from trader.config import FeesConfig
from trader.core.calendar import TradingCalendar, parse_alpaca_calendar
from trader.core.clock import ET, SimClock
from trader.core.models import OrderIntent, Quote, Trade
from trader.execution.records import Records
from trader.execution.service import FlattenResult, MarketHours, flatten_account
from trader.execution.sim_broker import SimBroker
from trader.memory.journal import Journal
from trader.risk.ledger import read_entries

D = Decimal
DAY = date(2026, 10, 9)
T0 = datetime(2026, 10, 9, 10, 0, tzinfo=ET)
HOURS: MarketHours = (
    datetime(2026, 10, 9, 9, 30, tzinfo=ET),
    datetime(2026, 10, 9, 16, 0, tzinfo=ET),
)
FIRST, LAST = date(2026, 9, 1), date(2026, 12, 31)
CAL = TradingCalendar(parse_alpaca_calendar(calendar_rows(FIRST, LAST)), FIRST, LAST)
FEES = FeesConfig(
    sec_fee_per_million_sold=D("20.60"),
    finra_taf_per_share_sold=D("0.000195"),
    finra_taf_max_per_trade=D("9.79"),
)


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


def quote(bid: str, ask: str) -> Quote:
    return Quote(symbol="XYZ", ts=T0, bid=D(bid), bid_size=5, ask=D(ask), ask_size=5)


async def hold(sim: SimBroker, records: Records) -> None:
    """A filled 40-share entry with its stop leg working, journaled like the engine would."""
    sim.feed(quote("12.30", "12.32"))
    accepted = await sim.submit_entry_oto(
        OrderIntent(
            book="A",
            signal_id="20261009-XYZ-orb5",
            symbol="XYZ",
            qty=40,
            entry_limit=D("12.35"),
            stop_price=D("12.21"),
            client_order_id="A-20261009-XYZ-entry-1",
            day_size_multiplier=D("1"),
        )
    )
    await records.apply(accepted, signal_id="20261009-XYZ-orb5")
    await records.apply(await sim.get_order(accepted.id))


async def flatten(
    sim: SimBroker, records: Records, clock: SimClock, **kwargs: object
) -> FlattenResult:
    options: dict[str, object] = {
        "deadline": clock.now() + timedelta(seconds=60),
        "hours": HOURS,
        "purpose": "manual",
    }
    options.update(kwargs)
    return await run_on_clock(
        clock,
        flatten_account(sim, records, clock=clock, **options),  # type: ignore[arg-type]
    )


async def statuses(journal: Journal) -> dict[str, str]:
    rows = await journal.fetch_all("SELECT client_order_id, status FROM orders")
    return {r["client_order_id"]: r["status"] for r in rows}


async def test_cancels_the_stop_then_sells_and_journals_everything(
    clock: SimClock, records: Records, journal: Journal
) -> None:
    sim = SimBroker(clock=clock)
    await hold(sim, records)
    result = await flatten(sim, records, clock)
    assert result == FlattenResult(flat=True)
    assert await sim.positions() == []
    journaled = await statuses(journal)
    assert journaled["A-20261009-XYZ-exit-1"] == "filled"
    assert "canceled" in journaled.values()  # the stop leg
    entries = await journal.write(lambda conn: read_entries(conn, "A"))
    assert [e.kind for e in entries] == ["buy", "sell", "fee"]
    assert sum(e.amount for e in entries if e.kind != "fee") == sim.cash - D(1000)


async def test_a_stop_that_fills_during_a_slow_cancel_is_booked(
    clock: SimClock, records: Records, journal: Journal
) -> None:
    sim = SimBroker(clock=clock)
    await hold(sim, records)
    sim.slow_cancels = timedelta(seconds=2)
    beats = 0

    async def beat() -> None:
        nonlocal beats
        beats += 1
        if beats == 2:  # the cancel is still pending: the price drops through the stop
            sim.feed(Trade(symbol="XYZ", ts=T0, price=D("12.10"), size=100))

    result = await flatten(sim, records, clock, heartbeat=beat)
    assert result.flat
    journaled = await statuses(journal)
    assert not any(cid.endswith("-exit-1") for cid in journaled)  # nothing left to sell
    entries = await journal.write(lambda conn: read_entries(conn, "A"))
    assert [e.kind for e in entries] == ["buy", "sell", "fee"]  # the stop's sale is booked


async def test_a_rejected_sell_is_retried_with_the_next_id(
    clock: SimClock, records: Records, journal: Journal
) -> None:
    sim = SimBroker(clock=clock)
    await hold(sim, records)
    sim.fail_next_submit("reject", "insufficient qty available for order")
    result = await flatten(sim, records, clock)
    assert result.flat
    journaled = await statuses(journal)
    assert (journaled["A-20261009-XYZ-exit-1"], journaled["A-20261009-XYZ-exit-2"]) == (
        "rejected",
        "filled",
    )


async def test_an_uncertain_sell_found_by_lookup_is_adopted(
    clock: SimClock, records: Records, journal: Journal
) -> None:
    sim = SimBroker(clock=clock)
    await hold(sim, records)
    sim.fail_next_submit("timeout_after")
    result = await flatten(sim, records, clock)
    assert result.flat
    assert (await statuses(journal))["A-20261009-XYZ-exit-1"] == "filled"
    assert sim.submits.count("A-20261009-XYZ-exit-1") == 1


async def test_an_exit_id_taken_elsewhere_is_adopted_not_rejected(
    clock: SimClock, records: Records, journal: Journal
) -> None:
    sim = SimBroker(clock=clock)
    await hold(sim, records)
    # Another process (say `trader flatten` while the engine runs) used exit-1 for 10 shares.
    entry = await records.order("A-20261009-XYZ-entry-1")
    assert entry is not None
    assert entry.broker_order_id is not None
    leg = (await sim.get_order(entry.broker_order_id)).stop_leg
    assert leg is not None
    await sim.cancel(leg.id)
    await sim.submit_market_sell("XYZ", 10, "A-20261009-XYZ-exit-1")
    result = await flatten(sim, records, clock)
    assert result.flat
    journaled = await statuses(journal)
    assert (journaled["A-20261009-XYZ-exit-1"], journaled["A-20261009-XYZ-exit-2"]) == (
        "filled",  # adopted after the duplicate-id answer
        "filled",  # the remaining 30
    )


async def test_reports_what_is_left_at_the_deadline(
    clock: SimClock, records: Records, journal: Journal
) -> None:
    sim = SimBroker(clock=clock, market_open=lambda _now: False)  # sells only queue
    await hold(sim, records)
    result = await flatten(sim, records, clock, deadline=clock.now() + timedelta(seconds=5))
    assert result == FlattenResult(flat=False, remaining={"XYZ": 40})


async def test_outside_market_hours_it_keeps_a_gtc_stop_instead(
    clock: SimClock, records: Records, journal: Journal
) -> None:
    sim = SimBroker(clock=clock)
    await hold(sim, records)
    sim.expire_day_orders()  # 16:00: the day stop leg is gone
    await clock.advance_to(datetime(2026, 10, 9, 16, 1, tzinfo=ET))
    result = await flatten(sim, records, clock)
    assert result == FlattenResult(
        flat=False, remaining={"XYZ": 40}, market_closed=True, protected={"XYZ": D("12.21")}
    )
    (stop,) = await sim.open_orders()
    assert (stop.type, stop.time_in_force, stop.qty, stop.stop_price) == (
        "stop",
        "gtc",
        40,
        D("12.21"),
    )
    again = await flatten(sim, records, clock)  # a working GTC stop is left alone...
    assert again.protected == {"XYZ": D("12.21")}  # ...and reported, so the P1 can name it
    assert len(await sim.open_orders()) == 1


async def test_after_the_close_a_slow_cancel_is_waited_for_before_the_gtc_stop(
    clock: SimClock, records: Records, journal: Journal
) -> None:
    sim = SimBroker(clock=clock)
    await hold(sim, records)  # its day stop leg still works: Alpaca hasn't expired it yet
    sim.slow_cancels = timedelta(seconds=2)  # the leg holds the shares until then
    await clock.advance_to(datetime(2026, 10, 9, 16, 1, tzinfo=ET))
    result = await flatten(sim, records, clock)
    assert result.protected == {"XYZ": D("12.21")}
    stops = [o for o in await sim.open_orders() if o.time_in_force == "gtc"]
    assert [(o.qty, o.stop_price) for o in stops] == [(40, D("12.21"))]  # all 40 covered


async def test_flat_already_is_quick(clock: SimClock, records: Records) -> None:
    assert await flatten(SimBroker(clock=clock), records, clock) == FlattenResult(flat=True)


# ---------------------------------------------------------------- the exit at work (D-053)


async def test_its_own_slow_market_sell_is_never_cancelled(
    clock: SimClock, records: Records, journal: Journal
) -> None:
    sim = SimBroker(clock=clock)
    await hold(sim, records)
    sim.accept_delay = timedelta(seconds=9.7)  # paper took 9.7 s to accept FIGS's exit
    result = await flatten(sim, records, clock)
    assert result.flat
    exits = [cid for cid in sim.submits if "-exit-" in cid]
    assert exits == ["A-20261009-XYZ-exit-1"]  # sold once
    entry = await sim.order_by_client_id("A-20261009-XYZ-entry-1")
    assert entry is not None
    leg = (await sim.get_order(entry.id)).stop_leg
    assert leg is not None
    assert sim.cancels == [leg.id]  # the stop leg only, never the exit
    assert (await statuses(journal))["A-20261009-XYZ-exit-1"] == "filled"


async def test_one_symbols_working_sell_does_not_hold_up_another(
    clock: SimClock, records: Records
) -> None:
    sim = SimBroker(clock=clock)
    sim.feed(Quote(symbol="ABC", ts=T0, bid=D("20.00"), bid_size=5, ask=D("20.02"), ask_size=5))
    sim.hold("ABC", 10, D("20.00"))  # bare shares, no orders
    sim.hold("XYZ", 40, D("12.30"))
    sim.feed(quote("12.30", "12.32"))
    sim.accept_delay = timedelta(seconds=9.7)
    manual = await sim.submit_market_sell("XYZ", 40, "manual-exit-1")  # e.g. `trader flatten`
    started = clock.now()
    result = await flatten(sim, records, clock)
    assert result.flat
    assert manual.id not in sim.cancels  # the other process's exit was left to fill
    abc = await records.order("A-20261009-ABC-exit-1")  # our send time: paper rewrites its own
    assert abc is not None
    assert abc.submitted_at is not None
    assert abc.submitted_at - started < timedelta(seconds=1)  # sold in the first pass


async def test_an_open_buy_blocks_only_its_own_symbol(
    clock: SimClock, records: Records, journal: Journal
) -> None:
    sim = SimBroker(clock=clock)
    sim.feed(Quote(symbol="ABC", ts=T0, bid=D("20.00"), bid_size=5, ask=D("20.02"), ask_size=5))
    sim.hold("ABC", 10, D("20.00"))
    sim.partial_next(15)
    await hold(sim, records)  # XYZ: 15 of 40 filled, the rest still open
    sim.feed(quote("12.40", "12.50"))  # the rest can't fill
    sim.slow_cancels = timedelta(seconds=2)  # the buy's cancel takes 2 s
    started = clock.now()
    result = await flatten(sim, records, clock)
    assert result.flat
    abc = await sim.order_by_client_id("A-20261009-ABC-exit-1")
    assert abc is not None
    assert abc.submitted_at - started < timedelta(seconds=1)  # not held up by XYZ's buy
    xyz = await sim.order_by_client_id("A-20261009-XYZ-exit-1")
    assert xyz is not None
    assert xyz.submitted_at - started >= timedelta(seconds=2)  # after the buy was final
    rows = await journal.fetch_all("SELECT 1 FROM orders WHERE status = 'rejected'")
    assert rows == []  # no sell was tried while the buy was open


async def test_a_market_sell_being_cancelled_is_waited_for(
    clock: SimClock, records: Records
) -> None:
    sim = SimBroker(clock=clock)
    sim.hold("XYZ", 40, D("12.30"))
    sim.feed(quote("12.30", "12.32"))
    sim.accept_delay = timedelta(seconds=5)
    manual = await sim.submit_market_sell("XYZ", 40, "manual-exit-1")
    await sim.cancel(manual.id)  # queued: at acceptance the sell fills first (FIGS's exit)
    sim.accept_delay = None
    result = await flatten(sim, records, clock)
    assert result.flat
    assert (await sim.get_order(manual.id)).status == "filled"
    assert [cid for cid in sim.submits if cid.startswith("A-")] == []  # nothing sold over it
    assert sim.cancels == [manual.id]  # never cancelled twice


async def test_after_the_close_a_queued_market_sell_is_still_cancelled(
    clock: SimClock, records: Records
) -> None:
    sim = SimBroker(clock=clock, market_open=lambda _now: False)
    await hold(sim, records)
    sim.expire_day_orders()  # 16:00: the day stop leg is gone
    await clock.advance_to(datetime(2026, 10, 9, 16, 1, tzinfo=ET))
    queued = await sim.submit_market_sell("XYZ", 40, "manual-exit-1")  # waits for the open
    assert queued.status == "accepted"
    result = await flatten(sim, records, clock)
    assert result.protected == {"XYZ": D("12.21")}
    assert (await sim.get_order(queued.id)).status == "canceled"
    stops = [o for o in await sim.open_orders() if o.time_in_force == "gtc"]
    assert [(o.qty, o.stop_price) for o in stops] == [(40, D("12.21"))]
