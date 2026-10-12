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
from trader.execution.broker import Order
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
