"""ExecutionService scenarios (P2.6) on SimBroker + SimClock + a real journal."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.fixtures.exec_harness import NEXT_OPEN, TIMES, Harness
from trader.core.clock import ET, SimClock
from trader.core.models import OrderIntent
from trader.execution import service as service_module
from trader.execution.broker import Order, OrderRejected
from trader.execution.sim_broker import SimBroker
from trader.risk.killswitch import KillSwitch

D = Decimal
ENTRY = "A-20261009-XYZ-entry-1"


@asynccontextmanager
async def running(tmp_path: Path, **options: Any) -> AsyncIterator[Harness]:
    harness = await Harness(tmp_path, **options).open()
    try:
        yield harness
    finally:
        await harness.close()


async def reasons(h: Harness, book: str = "A") -> list[str]:
    return [halt.reason for halt in await h.halts.active(book)]


async def sells(h: Harness) -> dict[str, tuple[str | None, str | None]]:
    """client_order_id -> (purpose, signal_id) of every sell journaled (D-044)."""
    rows = await h.journal.fetch_all(
        "SELECT client_order_id, purpose, signal_id FROM orders WHERE side = 'sell'"
    )
    return {r["client_order_id"]: (r["purpose"], r["signal_id"]) for r in rows}


SIGNAL = "20261009-XYZ-orb5"


# ---------------------------------------------------------------- the basic paths


async def test_happy_path(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.32")
        result = await h.enter()
        assert result.outcome == "submitted"
        await h.advance(6)
        assert await h.status(ENTRY) == "filled"
        assert await h.held() == {"XYZ": 40}
        assert await h.working_stops() == [(40, D("12.21"), "new")]
        assert await h.incidents() == []
        exposure = await h.service.exposure()
        assert (exposure.entries_today, exposure.unprotected) == (1, frozenset())
        assert exposure.available_cash == D(1000) - 40 * D("12.33")


async def test_stop_out_is_booked_as_a_loss(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.32")
        await h.enter()
        await h.advance(2)
        h.trade("12.15")  # gaps through the 12.21 stop: fills at 12.14
        await h.advance(2)
        assert await h.held() == {}
        exposure = await h.service.exposure()
        # 40 x (12.14 - 12.33) and a 2-cent fee estimate
        assert exposure.realized_pnl_today == D("-7.62")


async def test_an_entry_that_never_fills_is_cancelled_after_10_s(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.40")  # the ask is above the 12.35 limit
        await h.enter()
        await h.advance(9)
        assert await h.status(ENTRY) == "new"
        await h.advance(2)
        assert await h.status(ENTRY) == "canceled"
        assert await h.sim.open_orders() == []
        exposure = await h.service.exposure()
        assert exposure.entries_today == 0  # cancelled without a fill: doesn't count (R-007)
        assert exposure.symbols_entered_today == {"XYZ"}  # but the symbol is used up (R-008)


# ---------------------------------------------------------------- partial fills (D-032)


async def test_a_partial_fill_that_completes_within_the_grace(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.32")
        h.sim.partial_next(15)
        await h.enter()
        await h.advance(0.5)
        h.quote("12.31", "12.33")  # still marketable: the rest fills and the leg wakes up
        await h.advance(3)
        assert await h.held() == {"XYZ": 40}
        assert await h.working_stops() == [(40, D("12.21"), "new")]
        assert await h.incidents() == [("P3", "partial_fill_protected")]


async def test_a_partial_fill_cut_off_is_protected_within_5_s(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.32")
        h.sim.partial_next(15)
        await h.enter()
        h.quote("12.40", "12.50")  # moves away: the rest can't fill
        first_fill = h.clock.now()
        while not [stop for stop in await h.working_stops() if stop[2] == "new"]:
            await h.advance(0.25)  # the held leg protects nothing: wait for a live stop
            assert h.clock.now() - first_fill <= timedelta(seconds=5)
        assert await h.held() == {"XYZ": 15}
        assert await h.working_stops() == [(15, D("12.21"), "new")]
        assert await h.status(ENTRY) == "canceled"
        assert await h.incidents() == [("P3", "partial_fill_protected")]


async def test_the_entry_timeout_firing_just_after_a_partial_fill(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.40")
        await h.enter()
        await h.advance(9.5)
        h.sim.partial_next(15)
        h.quote("12.30", "12.34")  # 15 fill half a second before the 10 s timeout
        h.quote("12.40", "12.50")
        await h.advance(5)
        assert await h.held() == {"XYZ": 15}
        assert await h.working_stops() == [(15, D("12.21"), "new")]
        assert ("P1", "unprotected_position") not in await h.incidents()


async def test_a_held_leg_protects_once_its_entry_has_filled(tmp_path: Path) -> None:
    async with running(tmp_path, leg_status_after_fill="held") as h:
        h.quote("12.30", "12.32")
        await h.enter()
        await h.advance(6)
        assert await h.working_stops() == [(40, D("12.21"), "held")]
        assert await h.status("A-20261009-XYZ-stop-1") is None  # no extra stop was needed
        assert await h.incidents() == []


async def test_a_rejected_stop_leg_is_replaced_by_a_standalone_stop(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.sim.reject_stop_legs = True
        h.quote("12.30", "12.32")
        await h.enter()
        await h.advance(3)
        assert await h.working_stops() == [(40, D("12.21"), "new")]
        assert await h.status("A-20261009-XYZ-stop-1") == "new"
        assert ("P1", "unprotected_position") not in await h.incidents()
        tagged = await sells(h)
        assert tagged["A-20261009-XYZ-stop-1"] == ("protective_stop", SIGNAL)


async def test_already_through_the_stop_means_sell_now(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.sim.reject_stop_legs = True
        h.prices.last["XYZ"] = D("12.20")  # the hub's last trade is below the 12.21 stop
        h.quote("12.30", "12.32")
        await h.enter()
        await h.advance(3)
        assert await h.held() == {}
        assert await h.status("A-20261009-XYZ-exit-1") == "filled"
        assert await h.status("A-20261009-XYZ-stop-1") is None
        assert (await sells(h))["A-20261009-XYZ-exit-1"] == ("stop_breached", SIGNAL)


# ---------------------------------------------------------------- Alpaca paper at the open (D-053)


async def messages(h: Harness, code: str) -> list[str]:
    rows = await h.journal.fetch_all("SELECT message FROM incidents WHERE code = ?", (code,))
    return [r["message"] for r in rows]


async def entry_id(h: Harness) -> str:
    row = await h.records.order(ENTRY)
    assert row is not None
    assert row.broker_order_id is not None
    return row.broker_order_id


def reject_every_stop(h: Harness) -> None:
    async def refuse(*args: Any, **kwargs: Any) -> Order:
        raise OrderRejected("stop refused (test)", status=403)

    h.sim.submit_stop = refuse  # type: ignore[method-assign]


async def test_a_cancel_landing_after_5_s_leaves_time_for_the_stop(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.32")
        h.sim.partial_next(15)
        h.sim.slow_cancels = timedelta(seconds=7)  # the remainder's cancel takes 7 s
        await h.enter()
        h.quote("12.40", "12.50")  # moves away: the rest can't fill
        await h.advance(12)
        assert await h.held() == {"XYZ": 15}
        assert await h.working_stops() == [(15, D("12.21"), "new")]
        assert await h.status(ENTRY) == "canceled"
        assert await h.incidents() == [("P3", "partial_fill_protected")]


async def test_the_open_13_s_to_accept_and_4_s_to_cancel_is_not_a_p1(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.sim.accept_delay, h.sim.slow_cancels = timedelta(seconds=13), timedelta(seconds=4)
        h.quote("12.30", "12.32")
        h.sim.partial_next(13)
        await h.enter()
        await h.advance(13.25)  # accepted: 13 fill at once
        assert await h.held() == {"XYZ": 13}
        h.quote("12.40", "12.50")
        await h.advance(10)
        assert await h.held() == {"XYZ": 13}
        assert [(q, p) for q, p, _ in await h.working_stops()] == [(13, D("12.21"))]
        assert await h.incidents() == [("P3", "partial_fill_protected")]
        assert h.sim.cancels == [await entry_id(h)]  # the timeout never fired before acceptance


async def test_a_cancel_the_order_outlives_is_sent_again(tmp_path: Path) -> None:
    """FIGS at the open: the cancel sent before acceptance never took effect, and the order
    filled after it. Sent again once the order is working, the shares get their stop."""
    async with running(tmp_path) as h:
        h.sim.accept_delay = timedelta(seconds=35)  # past the 30 s acceptance limit
        h.sim.pending_new_cancels = "drop"  # the worst case: the queued cancel never takes
        h.sim.slow_cancels = timedelta(seconds=4)
        h.quote("12.30", "12.32")
        h.sim.partial_next(16)
        await h.enter()
        await h.advance(31)
        order_id = await entry_id(h)
        assert h.sim.cancels == [order_id]  # cancelled at 30 s, while still pending_new
        await h.advance(5)  # accepted at 35 s: 16 fill, the queued cancel is rejected
        h.quote("12.40", "12.50")
        await h.advance(10)
        assert h.sim.cancels.count(order_id) == 2  # sent again, once
        assert await h.status(ENTRY) == "canceled"
        assert await h.held() == {"XYZ": 16}
        assert [(q, p) for q, p, _ in await h.working_stops()] == [(16, D("12.21"))]
        assert await h.incidents() == [("P3", "partial_fill_protected")]


async def test_no_cancel_is_sent_again_while_the_order_waits_for_acceptance(
    tmp_path: Path,
) -> None:
    async with running(tmp_path) as h:
        h.sim.accept_delay = timedelta(seconds=50)
        h.quote("12.30", "12.40")  # not marketable
        await h.enter()
        await h.advance(49)
        assert h.sim.cancels == [await entry_id(h)]  # one, at 30 s: queued at the broker
        await h.advance(2)  # accepted at 50 s, and the queued cancel takes effect
        assert await h.status(ENTRY) == "canceled"
        assert len(h.sim.cancels) == 1


async def test_a_cancel_that_never_lands_is_a_p1_at_20_s_not_5(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.32")
        h.sim.partial_next(15)
        h.sim.slow_cancels = timedelta(seconds=120)
        await h.enter()
        h.quote("12.40", "12.50")
        await h.advance(19.5)
        assert await h.incidents() == []
        await h.advance(1)
        assert ("P1", "unprotected_position") in await h.incidents()
        assert "cancel was still pending" in (await messages(h, "unprotected_position"))[0]
        assert "p1_incident" in await reasons(h)


async def test_once_the_cancel_lands_the_stop_has_5_s(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.32")
        h.sim.partial_next(15)
        h.sim.slow_cancels = timedelta(seconds=6)  # sent at 1 s, lands at 7 s
        reject_every_stop(h)
        await h.enter()
        h.quote("12.40", "12.50")
        await h.advance(11.5)
        assert await h.incidents() == []
        await h.advance(1)
        assert ("P1", "unprotected_position") in await h.incidents()
        assert "cancel landed at" in (await messages(h, "unprotected_position"))[0]


async def test_without_a_pending_cancel_the_limit_is_still_5_s(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.sim.reject_stop_legs = True
        reject_every_stop(h)
        h.quote("12.30", "12.32")
        await h.enter()  # fills at once; its leg is rejected and no stop is accepted
        await h.advance(4.75)
        assert await h.incidents() == []
        await h.advance(0.75)
        assert ("P1", "unprotected_position") in await h.incidents()


async def test_someone_elses_pending_buy_gets_no_extra_time(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.sim.reject_stop_legs = True
        reject_every_stop(h)
        h.quote("12.30", "12.32")
        await h.enter()  # 40 bare shares
        h.quote("12.20", "12.25")  # a resting buy below the market
        resting = h.intent(
            client_order_id="manual-1",
            signal_id="manual",
            qty=1,
            entry_limit=D("12.10"),
            stop_price=D("11.90"),
        )
        foreign = await h.sim.submit_entry_oto(resting)
        h.sim.slow_cancels = timedelta(seconds=60)
        await h.sim.cancel(foreign.id)  # pending at the broker, but not ours
        await h.advance(5.5)
        assert ("P1", "unprotected_position") in await h.incidents()


async def test_the_entry_timeout_counts_from_acceptance(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.sim.accept_delay = timedelta(seconds=8)
        h.quote("12.30", "12.40")  # not marketable
        await h.enter()
        await h.advance(10.5)
        assert h.sim.cancels == []  # live for 2.5 s only
        assert await h.status(ENTRY) == "new"
        await h.advance(8.5)
        assert h.sim.cancels == [await entry_id(h)]
        assert await h.status(ENTRY) == "canceled"


async def test_an_entry_never_accepted_is_cancelled_at_30_s_and_reported_if_stuck(
    tmp_path: Path,
) -> None:
    async with running(tmp_path) as h:
        h.sim.accept_delay = timedelta(seconds=200)
        h.quote("12.30", "12.40")
        await h.enter()
        await h.advance(29.5)
        assert h.sim.cancels == []
        await h.advance(1)
        assert h.sim.cancels == [await entry_id(h)]
        assert (await h.service.exposure()).symbols == {"XYZ"}  # still counts while open
        await h.advance(60)
        assert await h.incidents() == [("P2", "entry_stuck")]
        await h.advance(110)  # accepted at 200 s: the queued cancel takes effect
        assert await h.status(ENTRY) == "canceled"
        assert (await h.service.exposure()).entries_today == 0


async def test_a_restart_does_not_give_an_entry_a_fresh_timeout(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.sim.accept_delay = timedelta(seconds=5)
        h.quote("12.30", "12.40")
        await h.enter()
        await h.service.stop()  # dies while the order waits for acceptance
        await h.clock.advance(timedelta(seconds=12))  # accepted at 5 s, while down
        await h.start()
        await h.advance(1)
        assert h.sim.cancels == [await entry_id(h)]  # past submission + 10 s: at once


async def test_a_restart_during_a_slow_cancel_waits_for_it(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.32")
        h.sim.partial_next(15)
        h.sim.slow_cancels = timedelta(seconds=10)  # sent at 1 s, lands at 11 s
        await h.enter()
        h.quote("12.40", "12.50")
        await h.advance(2)
        await h.service.stop()
        await h.clock.advance(timedelta(seconds=2))
        await h.start()
        await h.advance(14)
        assert ("P1", "unprotected_position") not in await h.incidents()
        assert [(q, p) for q, p, _ in await h.working_stops()] == [(15, D("12.21"))]


async def test_a_sell_on_its_way_out_counts_as_cover(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.sim.reject_stop_legs = True
        h.sim.accept_delay = timedelta(seconds=9)  # every order waits 9 s to be accepted
        h.prices.last["XYZ"] = D("12.20")  # already through the 12.21 stop: sell at once
        h.quote("12.30", "12.32")
        await h.enter()
        await h.advance(9.25)  # accepted: 40 fill, the leg is rejected, the exit is sent
        assert await h.held() == {"XYZ": 40}
        await h.advance(7)  # the exit waits for acceptance past the 5 s limit
        assert ("P1", "unprotected_position") not in await h.incidents()
        await h.advance(4)
        assert await h.held() == {}
        assert (await sells(h))["A-20261009-XYZ-exit-1"] == ("stop_breached", SIGNAL)


class LaggingList(SimBroker):
    """The order list trails get_order: an order that turns final stays listed, as it last
    was, for half a second (Alpaca's list lagged by 0.3-0.6 s on 2026-10-13)."""

    LAG = timedelta(seconds=0.5)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._listed: dict[str, Order] = {}
        self._gone: dict[str, datetime] = {}

    async def open_orders(self) -> list[Order]:
        now = self._clock.now()
        current = await super().open_orders()
        ids = {o.id for o in current}
        for order_id in list(self._listed):
            if order_id not in ids and now - self._gone.setdefault(order_id, now) >= self.LAG:
                del self._listed[order_id], self._gone[order_id]
        self._listed.update({o.id: o for o in current})
        return current + [o for order_id, o in self._listed.items() if order_id not in ids]


async def test_a_lagging_order_list_does_not_take_the_window_away(tmp_path: Path) -> None:
    async with running(tmp_path, sim_class=LaggingList) as h:
        h.quote("12.30", "12.32")
        h.sim.partial_next(15)
        h.sim.slow_cancels = timedelta(seconds=7)
        await h.enter()
        h.quote("12.40", "12.50")
        await h.advance(12)
        assert await h.incidents() == [("P3", "partial_fill_protected")]
        assert [(q, p) for q, p, _ in await h.working_stops()] == [(15, D("12.21"))]


class StuckSells(SimBroker):
    """A market sell that is accepted but never fills (a halted symbol, a paper backlog)."""

    def _fill_price(self, order: Any, trade: Any) -> Decimal | None:
        return None if order.type == "market" else super()._fill_price(order, trade)


async def test_a_stuck_exit_stops_counting_as_cover_after_20_s(tmp_path: Path) -> None:
    async with running(tmp_path, sim_class=StuckSells) as h:
        h.sim.reject_stop_legs = True
        h.prices.last["XYZ"] = D("12.20")  # through the stop: the exit goes out at once
        h.quote("12.30", "12.32")
        await h.enter()
        await h.advance(19)
        assert await h.incidents() == []  # the exit is on its way: covered for now
        await h.advance(8)
        assert ("P1", "unprotected_position") in await h.incidents()


# ---------------------------------------------------------------- uncertain submits (D-033)


async def test_an_uncertain_submit_that_landed_is_adopted(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.sim.fail_next_submit("timeout_after")
        h.quote("12.30", "12.32")
        result = await h.enter()
        assert result.outcome == "submitted"
        await h.advance(2)
        assert await h.status(ENTRY) == "filled"
        assert h.sim.submits.count(ENTRY) == 1


async def test_a_late_entry_is_adopted_never_sent_twice(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.sim.fail_next_submit("timeout_late", delay=timedelta(seconds=3))
        h.quote("12.30", "12.32")
        entering = asyncio.create_task(h.enter())
        await h.advance(1.5)
        assert (await entering).outcome == "uncertain"
        assert (await h.service.exposure()).symbols == {"XYZ"}  # still counted as pending
        await h.advance(4)
        assert await h.status(ENTRY) == "filled"  # it arrived late and filled
        assert await h.working_stops() == [(40, D("12.21"), "new")]
        assert h.sim.submits == [ENTRY]


async def test_an_entry_that_never_arrives_is_marked_lost(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.sim.fail_next_submit("timeout_before")
        h.quote("12.30", "12.32")
        entering = asyncio.create_task(h.enter())
        await h.advance(1.5)
        assert (await entering).outcome == "uncertain"
        await h.advance(60, step=2)
        assert await h.status(ENTRY) == "uncertain"  # still held as pending
        await h.advance(12, step=2)
        assert await h.status(ENTRY) == "lost"
        assert h.sim.submits == [ENTRY]
        exposure = await h.service.exposure()
        assert (exposure.entries_today, exposure.symbols) == (0, frozenset())


async def test_two_signals_in_the_same_second_share_one_slot(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.32")
        await h.enter()
        h.quote("20.00", "20.02", symbol="AAA")
        h.quote("30.00", "30.02", symbol="BBB")
        first, second = await asyncio.gather(
            h.enter("AAA", qty=10, entry_limit=D("20.05"), stop_price=D("19.90")),
            h.enter("BBB", qty=5, entry_limit=D("30.05"), stop_price=D("29.80")),
        )
        outcomes = sorted([first.outcome, second.outcome])
        assert outcomes == ["rejected", "submitted"]
        refused = first if first.outcome == "rejected" else second
        assert refused.check is not None
        assert "R-006" in refused.check.violations  # two positions is the limit


# ---------------------------------------------------------------- halts and the kill switch


class SlowSim(SimBroker):
    """Each entry submit takes 2 s to answer."""

    async def submit_entry_oto(self, intent: OrderIntent) -> Order:
        await self._clock.sleep_until(self._clock.now() + timedelta(seconds=2))
        return await super().submit_entry_oto(intent)


async def test_kill_during_a_submit_in_flight_is_flat_within_10_s(tmp_path: Path) -> None:
    async with running(tmp_path, sim_class=SlowSim) as h:
        h.quote("12.30", "12.32")
        entering = asyncio.create_task(h.enter())
        await h.settle()  # the submit is in flight

        async def flatten() -> bool:
            return (await h.service.flatten(purpose="kill_switch")).flat

        kill = KillSwitch(
            tmp_path / "KILL", halts=h.halts, clock=h.clock, books=("A",), flatten=flatten
        )
        (tmp_path / "KILL").write_text("", encoding="utf-8")
        killed_at = h.clock.now()
        killing = asyncio.create_task(kill.check())
        while not (killing.done() and entering.done()):
            await h.advance(0.5)
            assert h.clock.now() - killed_at <= timedelta(seconds=10)
        assert (await entering).outcome == "submitted"  # it landed, then was flattened
        assert await h.held() == {}
        assert await h.sim.open_orders() == []
        assert "kill_switch" in await reasons(h)
        refused = await h.enter("AAA")
        assert refused.outcome == "halted"


async def test_daily_loss_flattens_and_halts_until_the_next_session(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.32")
        await h.enter()
        await h.advance(2)
        h.prices.last["XYZ"] = D("11.50")  # marked $33.20 down: past 3% of $1,000
        await h.advance(6)
        assert await h.held() == {}
        assert ("P2", "daily_loss_limit") in await h.incidents()
        (daily,) = [x for x in await h.halts.active("A") if x.reason == "daily_loss"]
        assert daily.expires_at == NEXT_OPEN


async def test_flatten_at_1555_is_flat_well_before_1558(tmp_path: Path) -> None:
    clock = SimClock(datetime(2026, 10, 9, 15, 0, tzinfo=ET))
    async with running(tmp_path, clock=clock) as h:
        h.quote("12.30", "12.32")
        await h.enter()
        await h.advance(3300, step=30)  # quietly to 15:55
        assert await h.held() == {"XYZ": 40}
        flattening = asyncio.create_task(
            h.service.flatten(purpose="eod_flatten", deadline=TIMES.flat_by)
        )
        while not flattening.done():
            await h.advance(0.5)
        assert (await flattening).flat
        assert h.clock.now() < TIMES.flat_by
        tagged = await sells(h)
        assert tagged["A-20261009-XYZ-exit-1"] == ("eod_flatten", SIGNAL)
        assert sorted(purpose for purpose, _ in tagged.values()) == ["eod_flatten", "stop_leg"]


async def test_flattens_run_one_at_a_time(tmp_path: Path, monkeypatch: Any) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.32")
        await h.enter()
        await h.advance(2)
        active = peak = 0
        real = service_module.flatten_account

        async def counting(*args: Any, **kwargs: Any) -> Any:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                return await real(*args, **kwargs)
            finally:
                active -= 1

        monkeypatch.setattr(service_module, "flatten_account", counting)
        kill = asyncio.create_task(h.service.flatten(purpose="kill_switch"))
        eod = asyncio.create_task(h.service.flatten(purpose="eod_flatten"))
        while not (kill.done() and eod.done()):
            await h.advance(0.5)
        assert peak == 1  # the second waited for the first
        assert (await kill).flat
        assert (await eod).flat  # and found the book already flat
        assert sorted(p for p, _ in (await sells(h)).values()) == ["kill_switch", "stop_leg"]


class Flaky(SimBroker):
    """Its next position read blows up with something no handler expects."""

    explode = False

    async def positions(self) -> list[Any]:
        if self.explode:
            self.explode = False
            raise RuntimeError("unreadable payload")
        return list(await super().positions())


async def test_protection_outlives_an_unexpected_error(tmp_path: Path) -> None:
    async with running(tmp_path, sim_class=Flaky) as h:
        assert isinstance(h.sim, Flaky)
        h.sim.explode = True
        h.sim.reject_stop_legs = True
        h.quote("12.30", "12.32")
        await h.enter()
        await h.advance(3)
        assert await h.status("A-20261009-XYZ-stop-1") == "new"  # the loop went on


# ---------------------------------------------------------------- the stream and recovery


async def test_a_quiet_stream_cannot_hide_a_fill(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.sim.pause_stream()  # a half-dead socket
        h.quote("12.30", "12.32")
        await h.enter()
        await h.advance(3)
        assert await h.status(ENTRY) == "filled"  # found by polling
        assert (await h.service.exposure()).unprotected == frozenset()
        h.sim.resume_stream(drop=True)  # reconnect: missed events are never replayed
        await h.advance(3)
        assert await h.incidents() == []


async def test_a_restart_adopts_the_position_and_its_stop(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.32")
        await h.enter()
        await h.advance(2)
        await h.service.stop()  # the engine dies
        await h.start()
        await h.advance(6)
        assert await h.working_stops() == [(40, D("12.21"), "new")]
        assert await h.status("A-20261009-XYZ-stop-1") is None
        assert await h.incidents() == []


async def test_a_restart_finds_a_naked_partial_and_acts_at_once(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        h.quote("12.30", "12.32")
        h.sim.partial_next(15)
        await h.enter()
        h.quote("12.40", "12.50")
        await h.service.stop()  # dies before the grace runs out
        await h.clock.advance(timedelta(seconds=8))  # down for 8 s: past the 5 s limit
        await h.start()
        await h.advance(3)
        assert ("P1", "unprotected_position") in await h.incidents()
        assert await h.held() == {}
        assert "p1_incident" in await reasons(h)


# ---------------------------------------------------------------- reconciliation (D-037)


async def place_by_hand(h: Harness) -> Order:
    """Someone buys 2 ZZZ in this account outside the bot."""
    h.quote("50.00", "50.02", symbol="ZZZ")
    return await h.sim.submit_entry_oto(
        OrderIntent(
            book="A",
            signal_id="manual",
            symbol="ZZZ",
            qty=2,
            entry_limit=D("50.05"),
            stop_price=D("49.50"),
            client_order_id="manual-1",
            day_size_multiplier=D("1"),
        )
    )


async def reconcile_now(h: Harness) -> None:
    h.service._resync = True  # what a StreamResync does
    h.service._wake.set()
    await h.settle()


async def test_a_difference_seen_once_does_not_halt(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        manual = await place_by_hand(h)
        await reconcile_now(h)
        leg = (await h.sim.get_order(manual.id)).stop_leg
        assert leg is not None
        await h.sim.cancel(leg.id)  # gone before the second look
        await h.sim.submit_market_sell("ZZZ", 2, "manual-2")
        await h.advance(6)
        assert await reasons(h) == []
        assert await h.incidents() == []


async def test_a_difference_seen_twice_halts_both_books(tmp_path: Path) -> None:
    async with running(tmp_path) as h:
        await place_by_hand(h)
        await reconcile_now(h)
        await h.advance(6)
        assert {"reconcile_mismatch", "p1_incident"} <= set(await reasons(h))
        assert await reasons(h, "B") == ["reconcile_mismatch"]
        assert ("P1", "reconcile_mismatch") in await h.incidents()
        assert await h.held() == {}  # a P1 flattens the affected book
