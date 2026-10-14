import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from trader.core.clock import ET, SimClock
from trader.core.models import OrderIntent, Quote, Trade
from trader.execution.broker import (
    BrokerEvent,
    DuplicateClientOrderId,
    OrderNotCancelable,
    OrderRejected,
    StreamResync,
    SubmitUncertain,
    TradeUpdate,
    stop_is_working,
)
from trader.execution.sim_broker import SimBroker

T0 = datetime(2026, 10, 9, 9, 40, tzinfo=ET)
D = Decimal


def quote(bid: str, ask: str, symbol: str = "F") -> Quote:
    return Quote(symbol=symbol, ts=T0, bid=D(bid), bid_size=5, ask=D(ask), ask_size=5)


def trade(price: str, symbol: str = "F") -> Trade:
    return Trade(symbol=symbol, ts=T0, price=D(price), size=100)


def intent(
    qty: int = 40, limit: str = "12.35", stop: str = "12.21", n: int = 1, symbol: str = "F"
) -> OrderIntent:
    return OrderIntent(
        book="A",
        signal_id=f"20261009-{symbol}-orb5",
        symbol=symbol,
        qty=qty,
        entry_limit=D(limit),
        stop_price=D(stop),
        client_order_id=f"A-20261009-{symbol}-entry-{n}",
        day_size_multiplier=D("1.0"),
    )


@pytest.fixture
def clock() -> SimClock:
    return SimClock(T0)


@pytest.fixture
def sim(clock: SimClock) -> SimBroker:
    broker = SimBroker(clock=clock, slippage=D("0.01"))
    broker.feed(quote("12.30", "12.32"))
    return broker


async def drain(sim: SimBroker) -> list[BrokerEvent]:
    """Every event emitted so far, after the opening resync."""
    events: list[BrokerEvent] = []
    stream: AsyncIterator[BrokerEvent] = sim.trade_updates()
    first = await anext(stream)
    assert isinstance(first, StreamResync)
    while True:
        try:
            events.append(await asyncio.wait_for(anext(stream), 0.01))
        except TimeoutError:
            return events


def kinds(events: list[BrokerEvent]) -> list[tuple[str, str]]:
    return [(e.event, e.order.type) for e in events if isinstance(e, TradeUpdate)]


# ---------------------------------------------------------------- entries and legs


async def test_marketable_entry_fills_at_ask_plus_slippage_and_arms_its_leg(
    sim: SimBroker,
) -> None:
    accepted = await sim.submit_entry_oto(intent())
    assert accepted.status == "new"  # the answer comes before the fill, as at Alpaca
    leg = accepted.stop_leg
    assert leg is not None
    assert leg.status == "held"
    entry = await sim.get_order(accepted.id)
    assert (entry.status, entry.filled_qty, entry.filled_avg_price) == ("filled", 40, D("12.33"))
    assert entry.stop_leg is not None
    assert stop_is_working(entry.stop_leg, entry)
    assert sim.cash == D(1000) - 40 * D("12.33")
    assert kinds(await drain(sim)) == [("new", "limit"), ("fill", "limit"), ("new", "stop")]


async def test_leg_can_stay_held_after_the_fill(clock: SimClock) -> None:
    sim = SimBroker(clock=clock, leg_status_after_fill="held")
    sim.feed(quote("12.30", "12.32"))
    accepted = await sim.submit_entry_oto(intent())
    entry = await sim.get_order(accepted.id)
    assert entry.stop_leg is not None
    assert entry.stop_leg.status == "held"
    assert stop_is_working(entry.stop_leg, entry)  # held, but its entry is filled


async def test_non_marketable_entry_rests_until_the_price_comes_down(sim: SimBroker) -> None:
    accepted = await sim.submit_entry_oto(intent(limit="12.10", stop="11.90"))
    assert (await sim.get_order(accepted.id)).status == "new"
    sim.feed(trade("12.08"))
    entry = await sim.get_order(accepted.id)
    assert (entry.status, entry.filled_avg_price) == ("filled", D("12.09"))


async def test_a_partial_fill_leaves_the_leg_inactive(sim: SimBroker) -> None:
    sim.partial_next(15)
    accepted = await sim.submit_entry_oto(intent())
    entry = await sim.get_order(accepted.id)
    assert (entry.status, entry.filled_qty) == ("partially_filled", 15)
    assert entry.stop_leg is not None
    assert not stop_is_working(entry.stop_leg, entry)
    (position,) = await sim.positions()
    assert position.qty == 15
    sim.feed(quote("12.31", "12.33"))  # still marketable: the rest fills
    entry = await sim.get_order(accepted.id)
    assert (entry.status, entry.filled_qty) == ("filled", 40)


async def test_cancelling_a_partial_entry_kills_the_leg_and_leaves_shares_bare(
    sim: SimBroker,
) -> None:
    sim.partial_next(15)
    accepted = await sim.submit_entry_oto(intent())
    await sim.cancel(accepted.id)
    entry = await sim.get_order(accepted.id)
    assert (entry.status, entry.filled_qty) == ("canceled", 15)
    assert entry.stop_leg is not None
    assert entry.stop_leg.status == "canceled"
    (position,) = await sim.positions()
    assert (position.qty, position.qty_available) == (15, 15)  # nothing protects them
    assert await sim.open_orders() == []


async def test_stop_triggers_on_a_trade_and_fills_below_it(sim: SimBroker) -> None:
    await sim.submit_entry_oto(intent())
    sim.feed(trade("12.25"))
    assert (await sim.positions())[0].qty == 40
    sim.feed(trade("12.18"))  # gaps through the 12.21 stop
    assert await sim.positions() == []
    assert sim.cash == D(1000) - 40 * D("12.33") + 40 * (D("12.18") - D("0.01"))


async def test_a_rejected_stop_leg_leaves_the_position_bare(clock: SimClock) -> None:
    sim = SimBroker(clock=clock)
    sim.reject_stop_legs = True
    sim.feed(quote("12.30", "12.32"))
    accepted = await sim.submit_entry_oto(intent())
    entry = await sim.get_order(accepted.id)
    assert entry.stop_leg is not None
    assert entry.stop_leg.status == "rejected"
    assert ("rejected", "stop") in kinds(await drain(sim))


# ---------------------------------------------------------------- Alpaca's protections


async def test_a_working_stop_holds_the_shares(sim: SimBroker) -> None:
    accepted = await sim.submit_entry_oto(intent())
    with pytest.raises(OrderRejected, match="insufficient qty") as info:
        await sim.submit_market_sell("F", 40, "A-20261009-F-exit-1")
    assert info.value.code == 40310000
    leg = (await sim.get_order(accepted.id)).stop_leg
    assert leg is not None
    await sim.cancel(leg.id)
    sold = await sim.submit_market_sell("F", 40, "A-20261009-F-exit-1")
    assert (await sim.get_order(sold.id)).filled_avg_price == D("12.29")  # bid - slippage
    assert await sim.positions() == []


async def test_wash_trade_rules(sim: SimBroker) -> None:
    sim.partial_next(15)
    await sim.submit_entry_oto(intent())  # partially filled and still open
    with pytest.raises(OrderRejected, match="wash trade"):
        await sim.submit_stop("F", 15, D("12.21"), "A-20261009-F-stop-1")
    with pytest.raises(OrderRejected, match="wash trade"):
        await sim.submit_market_sell("F", 15, "A-20261009-F-exit-1")


async def test_an_open_stop_blocks_a_new_buy(sim: SimBroker) -> None:
    sim.partial_next(15)
    accepted = await sim.submit_entry_oto(intent())
    await sim.cancel(accepted.id)
    await sim.submit_stop("F", 15, D("12.21"), "A-20261009-F-stop-1")
    with pytest.raises(OrderRejected, match="wash trade"):
        await sim.submit_entry_oto(intent(n=2))


async def test_buying_power_is_cash_minus_open_buys(sim: SimBroker) -> None:
    await sim.submit_entry_oto(intent(qty=60, limit="12.10", stop="11.90"))  # rests: $726
    sim.feed(quote("12.30", "12.32", symbol="G"))
    with pytest.raises(OrderRejected, match="buying power") as info:
        await sim.submit_entry_oto(intent(qty=30, limit="12.10", stop="11.90", symbol="G"))
    assert info.value.code == 40310000
    assert (await sim.account()).buying_power == D(1000) - 60 * D("12.10")


async def test_duplicate_ids_and_bad_prices(sim: SimBroker) -> None:
    await sim.submit_entry_oto(intent())
    with pytest.raises(DuplicateClientOrderId):
        await sim.submit_entry_oto(intent())
    with pytest.raises(OrderRejected) as info:
        await sim.submit_entry_oto(intent(limit="12.355", n=2))
    assert info.value.status == 422


async def test_stop_must_sit_below_the_current_price(sim: SimBroker) -> None:
    with pytest.raises(OrderRejected, match="current price"):
        await sim.submit_entry_oto(intent(limit="12.40", stop="12.32"))  # ask is 12.32


async def test_cannot_sell_shares_it_does_not_hold(sim: SimBroker) -> None:
    with pytest.raises(OrderRejected, match="insufficient qty"):
        await sim.submit_market_sell("F", 1, "A-20261009-F-exit-1")


# ---------------------------------------------------------------- faults


async def test_timeout_after_acceptance_leaves_an_order_to_find(sim: SimBroker) -> None:
    sim.fail_next_submit("timeout_after")
    with pytest.raises(SubmitUncertain):
        await sim.submit_entry_oto(intent())
    found = await sim.order_by_client_id("A-20261009-F-entry-1")
    assert found is not None
    assert found.status == "filled"


async def test_timeout_before_acceptance_leaves_nothing(sim: SimBroker) -> None:
    sim.fail_next_submit("timeout_before")
    with pytest.raises(SubmitUncertain):
        await sim.submit_entry_oto(intent())
    assert await sim.order_by_client_id("A-20261009-F-entry-1") is None
    assert sim.submits == ["A-20261009-F-entry-1"]


async def test_a_late_order_appears_after_its_delay(sim: SimBroker, clock: SimClock) -> None:
    sim.fail_next_submit("timeout_late", delay=timedelta(seconds=3))
    with pytest.raises(SubmitUncertain):
        await sim.submit_entry_oto(intent())
    assert await sim.order_by_client_id("A-20261009-F-entry-1") is None
    await clock.advance(timedelta(seconds=3))
    late = await sim.order_by_client_id("A-20261009-F-entry-1")
    assert late is not None
    assert late.status == "filled"  # it arrived and filled; cancelling can't undo that


async def test_injected_rejection(sim: SimBroker) -> None:
    sim.fail_next_submit("reject", "no")
    with pytest.raises(OrderRejected, match="no"):
        await sim.submit_entry_oto(intent())


async def test_a_slow_cancel_can_lose_a_race_with_a_fill(sim: SimBroker, clock: SimClock) -> None:
    sim.partial_next(15)
    accepted = await sim.submit_entry_oto(intent(limit="12.35"))
    sim.slow_cancels = timedelta(seconds=1)
    await sim.cancel(accepted.id)
    assert (await sim.get_order(accepted.id)).status == "pending_cancel"
    sim.feed(quote("12.30", "12.32"))  # fills the rest while the cancel is pending
    entry = await sim.get_order(accepted.id)
    assert (entry.status, entry.filled_qty) == ("filled", 40)
    await clock.advance(timedelta(seconds=1))
    assert (await sim.get_order(accepted.id)).status == "filled"
    with pytest.raises(OrderNotCancelable):
        await sim.cancel(accepted.id)


async def test_day_orders_expire_but_gtc_stops_survive(sim: SimBroker) -> None:
    sim.partial_next(15)
    accepted = await sim.submit_entry_oto(intent())
    await sim.cancel(accepted.id)
    await sim.submit_stop("F", 15, D("12.21"), "A-20261009-F-stop-1", tif="gtc")
    sim.feed(quote("20.00", "20.02", symbol="G"))
    resting = await sim.submit_entry_oto(intent(qty=1, limit="19.50", stop="19.00", symbol="G"))
    sim.expire_day_orders()
    assert (await sim.get_order(resting.id)).status == "expired"
    (stop,) = await sim.open_orders()
    assert (stop.type, stop.time_in_force) == ("stop", "gtc")


async def test_a_market_sell_after_the_close_is_only_queued(clock: SimClock) -> None:
    sim = SimBroker(clock=clock, market_open=lambda _now: False)
    sim.feed(quote("12.30", "12.32"))
    accepted = await sim.submit_entry_oto(intent())
    leg = (await sim.get_order(accepted.id)).stop_leg
    assert leg is not None
    await sim.cancel(leg.id)
    sold = await sim.submit_market_sell("F", 40, "A-20261009-F-exit-1")
    assert (await sim.get_order(sold.id)).status == "accepted"
    assert (await sim.positions())[0].qty == 40


async def test_a_paused_stream_holds_events_then_drops_or_reorders(sim: SimBroker) -> None:
    sim.pause_stream()
    await sim.submit_entry_oto(intent())
    sim.resume_stream(reverse=True)
    assert kinds(await drain(sim)) == [("new", "stop"), ("fill", "limit"), ("new", "limit")]
    sim.pause_stream()
    sim.feed(trade("12.10"))  # the stop fills, but the stream is down
    sim.resume_stream(drop=True)
    stream = sim.trade_updates()
    await anext(stream)  # the opening resync
    assert isinstance(await anext(stream), StreamResync)  # the reconnect


async def test_seeded_partial_fills_are_reproducible(clock: SimClock) -> None:
    async def run(seed: int) -> list[int]:
        sim = SimBroker(clock=clock, partial_fill_rate=0.5, seed=seed, cash=D(100_000))
        sim.feed(quote("12.30", "12.32"))
        filled = []
        for n in range(1, 9):
            symbol = f"S{chr(64 + n)}"  # one symbol each: a working leg blocks a second buy
            sim.feed(quote("12.30", "12.32", symbol=symbol))
            accepted = await sim.submit_entry_oto(intent(qty=10, symbol=symbol))
            filled.append((await sim.get_order(accepted.id)).filled_qty)
        return filled

    first = await run(7)
    assert first == await run(7)
    assert any(qty < 10 for qty in first)  # some entries were only partly filled


# ---------------------------------------------------------------- Alpaca paper at the open (D-053)


async def test_an_order_waits_in_pending_new_until_accepted(
    sim: SimBroker, clock: SimClock
) -> None:
    sim.accept_delay = timedelta(seconds=13)
    accepted = await sim.submit_entry_oto(intent())
    assert accepted.status == "pending_new"
    sim.feed(quote("12.30", "12.32"))  # marketable, but not yet at the venue
    assert (await sim.get_order(accepted.id)).filled_qty == 0
    await clock.advance(timedelta(seconds=13))
    entry = await sim.get_order(accepted.id)
    assert (entry.status, entry.filled_qty) == ("filled", 40)  # accepted, then filled
    assert kinds(await drain(sim)) == [
        ("pending_new", "limit"),
        ("new", "limit"),
        ("fill", "limit"),
        ("new", "stop"),
    ]


async def test_a_cancel_before_acceptance_is_queued_until_after_it(
    sim: SimBroker, clock: SimClock
) -> None:
    """RNG on 2026-10-13: one cancel while pending_new; the order showed new, then was
    cancelled seconds after acceptance."""
    sim.accept_delay, sim.queued_cancel_delay = timedelta(seconds=3), timedelta(seconds=9.6)
    accepted = await sim.submit_entry_oto(intent(limit="12.10", stop="11.90"))  # not marketable
    await clock.advance(timedelta(seconds=1))
    await sim.cancel(accepted.id)
    assert (await sim.get_order(accepted.id)).status == "pending_cancel"
    await clock.advance(timedelta(seconds=2))  # accepted: the queued cancel shows new
    assert (await sim.get_order(accepted.id)).status == "new"
    await clock.advance(timedelta(seconds=9.5))
    assert (await sim.get_order(accepted.id)).status == "new"
    await clock.advance(timedelta(seconds=0.1))
    entry = await sim.get_order(accepted.id)
    assert entry.status == "canceled"
    assert entry.stop_leg is not None
    assert entry.stop_leg.status == "canceled"
    assert sim.cancels == [accepted.id]


async def test_a_queued_cancel_that_finds_the_order_filled_is_rejected(
    sim: SimBroker, clock: SimClock
) -> None:
    sim.accept_delay, sim.queued_cancel_delay = timedelta(seconds=3), timedelta(seconds=5)
    accepted = await sim.submit_entry_oto(intent())  # marketable once accepted
    await sim.cancel(accepted.id)
    await clock.advance(timedelta(seconds=8))
    assert (await sim.get_order(accepted.id)).status == "filled"
    events = [e for e in await drain(sim) if isinstance(e, TradeUpdate)]
    rejected = [e for e in events if e.event == "order_cancel_rejected"]
    assert [e.order.status for e in rejected] == ["filled"]


async def test_drop_mode_never_cancels(sim: SimBroker, clock: SimClock) -> None:
    sim.accept_delay, sim.pending_new_cancels = timedelta(seconds=3), "drop"
    accepted = await sim.submit_entry_oto(intent(limit="12.10", stop="11.90"))
    await sim.cancel(accepted.id)
    await clock.advance(timedelta(seconds=30))
    assert (await sim.get_order(accepted.id)).status == "new"  # still working
    events = [e for e in await drain(sim) if isinstance(e, TradeUpdate)]
    assert [e.order.status for e in events if e.event == "order_cancel_rejected"] == ["new"]


async def test_a_cancel_after_acceptance_overtakes_the_queued_one(
    sim: SimBroker, clock: SimClock
) -> None:
    """FIGS on 2026-10-13: the queued cancel was still waiting when a second, sent to the
    accepted order, cancelled it; the queued one then came back rejected."""
    sim.accept_delay, sim.queued_cancel_delay = timedelta(seconds=3), timedelta(seconds=10)
    sim.slow_cancels = timedelta(seconds=4)
    sim.partial_next(13)
    accepted = await sim.submit_entry_oto(intent())
    await sim.cancel(accepted.id)  # queued
    await clock.advance(timedelta(seconds=3))  # accepted: 13 fill at once
    entry = await sim.get_order(accepted.id)
    assert (entry.status, entry.filled_qty) == ("partially_filled", 13)
    await clock.advance(timedelta(seconds=1))
    await sim.cancel(accepted.id)  # the second cancel, to a working order
    assert (await sim.get_order(accepted.id)).status == "pending_cancel"
    await clock.advance(timedelta(seconds=4))
    entry = await sim.get_order(accepted.id)
    assert (entry.status, entry.filled_qty) == ("canceled", 13)
    await clock.advance(timedelta(seconds=10))
    assert (await sim.get_order(accepted.id)).status == "canceled"  # the queued one is due
    events = [e for e in await drain(sim) if isinstance(e, TradeUpdate)]
    assert [e.order.status for e in events if e.event == "order_cancel_rejected"] == ["canceled"]
    assert sim.cancels == [accepted.id, accepted.id]


async def test_a_fill_during_a_slow_cancel_keeps_the_cancel(
    sim: SimBroker, clock: SimClock
) -> None:
    accepted = await sim.submit_entry_oto(intent(limit="12.10", stop="11.90"))  # resting
    sim.slow_cancels = timedelta(seconds=2)
    await sim.cancel(accepted.id)
    sim.partial_next(15)
    sim.feed(trade("12.08"))  # 15 fill while the cancel is pending
    entry = await sim.get_order(accepted.id)
    assert (entry.status, entry.filled_qty) == ("pending_cancel", 15)
    await clock.advance(timedelta(seconds=2))
    entry = await sim.get_order(accepted.id)
    assert (entry.status, entry.filled_qty) == ("canceled", 15)  # not lost to the fill


async def test_a_pending_new_market_sell_holds_its_shares(sim: SimBroker, clock: SimClock) -> None:
    sim.hold("F", 16, D("12.00"))
    sim.accept_delay = timedelta(seconds=9.7)
    sold = await sim.submit_market_sell("F", 16, "A-20261009-F-exit-1")
    assert sold.status == "pending_new"
    (position,) = await sim.positions()
    assert (position.qty, position.qty_available) == (16, 0)
    await clock.advance(timedelta(seconds=9.7))
    assert (await sim.get_order(sold.id)).status == "filled"
    assert await sim.positions() == []


async def test_an_order_that_turns_final_before_acceptance_stays_final(
    sim: SimBroker, clock: SimClock
) -> None:
    sim.accept_delay = timedelta(seconds=5)
    accepted = await sim.submit_entry_oto(intent())
    sim.expire_day_orders()  # the session ends while it waits
    await clock.advance(timedelta(seconds=5))
    entry = await sim.get_order(accepted.id)
    assert (entry.status, entry.filled_qty) == ("expired", 0)
    assert await sim.positions() == []


async def test_a_market_sell_accepted_after_the_close_waits_for_the_open(
    clock: SimClock,
) -> None:
    sim = SimBroker(clock=clock, market_open=lambda now: now < T0 + timedelta(seconds=3))
    sim.feed(quote("12.30", "12.32"))
    sim.hold("F", 16, D("12.00"))
    sim.accept_delay = timedelta(seconds=5)
    sold = await sim.submit_market_sell("F", 16, "A-20261009-F-exit-1")  # before the close
    await clock.advance(timedelta(seconds=5))  # accepted after it
    assert (await sim.get_order(sold.id)).status == "accepted"  # queued, not filled
    assert (await sim.positions())[0].qty == 16
