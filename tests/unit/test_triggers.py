"""Entry triggers (S§3.3, P3.2; DECISIONS D-043)."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trader.core.clock import ET, SimClock
from trader.core.models import Quote, Signal, Snapshot, Trade, TriggerEvent
from trader.memory.journal import Journal
from trader.strategy.orb_sip import load_signals, save_signals
from trader.strategy.triggers import TriggerEngine, counts_for_price, next_batch

D = Decimal
DAY = date(2026, 10, 13)
OR_END = datetime.combine(DAY, time(9, 35), ET)
ARM_AT = datetime.combine(DAY, time(9, 35, 5), ET)
LAST = datetime.combine(DAY, time(15, 30), ET)


def sig(symbol: str, rank: int, *, direction: str = "long", status: str = "armed",
        triggered_at: datetime | None = None) -> Signal:  # fmt: skip
    long = direction == "long"
    return Signal(
        id=f"20261013-{symbol}-orb5", date=DAY, symbol=symbol, direction=direction,  # type: ignore[arg-type]
        status=status, rel_vol=10.0 - rank, rel_vol_rank=rank,  # type: ignore[arg-type]
        or_open=D("12.40"), or_high=D("12.71"), or_low=D("12.30"), or_close=D("12.65"),
        or_volume=50_000, atr14=D("0.80"), adv14=2_000_000, prev_close=D("12.00"), gap_pct=3.3,
        trigger_price=D("12.72"), stop_price=D("12.64") if long else D("12.37"),
        max_chase_price=D("12.75") if long else None, entry_limit=D("12.74") if long else None,
        triggered_at=triggered_at,
    )  # fmt: skip


def trade(symbol: str, price: str, *, ts: datetime = ARM_AT, cond: tuple[str, ...] = ("@",),
          size: int = 100) -> Trade:  # fmt: skip
    return Trade(symbol=symbol, ts=ts, price=D(price), size=size, conditions=cond)


def snap(t: Trade) -> Snapshot:
    return Snapshot(symbol=t.symbol, latest_trade=t)


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(tmp_path / "trader.db")
    yield opened
    await opened.aclose()


def engine(journal: Journal, clock: SimClock) -> TriggerEngine:
    return TriggerEngine(clock=clock, journal=journal, books=("A", "B"), window_start=OR_END,
                         last_entry=LAST)  # fmt: skip


async def statuses(journal: Journal) -> dict[str, tuple[str, bool, bool]]:
    """symbol -> (status, armed_at set, triggered_at set)."""
    return {s.symbol: (s.status, s.armed_at is not None, s.triggered_at is not None)
            for s in await load_signals(journal, DAY)}  # fmt: skip


def drain(queue: "asyncio.Queue[TriggerEvent]") -> list[TriggerEvent]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_only_trades_that_set_bar_highs_count() -> None:
    assert counts_for_price(trade("X", "1", cond=("@",)))
    assert counts_for_price(trade("X", "1", cond=()))
    assert counts_for_price(trade("X", "1", cond=("@", "F")))  # ISO sweep: counts
    assert counts_for_price(trade("X", "1", cond=("Q9",)))  # unknown codes count
    assert not counts_for_price(trade("X", "1", cond=("@", "I")))  # odd lot: strictest wins
    assert not counts_for_price(trade("X", "1", cond=("Z",)))  # sold out of sequence


async def test_arming_applies_the_snapshot_check(journal: Journal) -> None:
    clock = SimClock(ARM_AT)
    signals = [sig("NOW", 1), sig("GONE", 2), sig("WAIT", 3), sig("OLD", 4), sig("ODD", 5),
               sig("SHRT", 6, direction="shadow_short", status="not_armed")]  # fmt: skip
    await save_signals(journal, signals)
    eng = engine(journal, clock)
    early = OR_END - timedelta(seconds=1)
    snaps = {
        "NOW": snap(trade("NOW", "12.74")),  # above the trigger, inside the chase cap
        "GONE": snap(trade("GONE", "12.80")),  # past the 12.75 cap
        "WAIT": snap(trade("WAIT", "12.60")),
        "OLD": snap(trade("OLD", "12.90", ts=early)),  # a print from inside the opening range
        "ODD": snap(trade("ODD", "12.74", cond=("@", "I"))),
    }
    result = await eng.arm(signals, snaps)
    assert [s.symbol for s in result.armed] == ["WAIT", "OLD", "ODD"]
    assert [ev.symbol for ev in result.triggered] == ["NOW"]
    assert [s.symbol for s in result.missed] == ["GONE"]
    assert eng.armed_symbols == ["ODD", "OLD", "WAIT"]
    assert await statuses(journal) == {
        "NOW": ("triggered", True, True), "GONE": ("missed_at_arm", True, False),
        "WAIT": ("armed", True, False), "OLD": ("armed", True, False),
        "ODD": ("armed", True, False),
        "SHRT": ("not_armed", False, False),
    }  # fmt: skip
    for book in ("A", "B"):
        (ev,) = drain(eng.queues[book])  # type: ignore[index]
        assert (ev.symbol, ev.source, ev.price, ev.received_at) == (
            "NOW",
            "arm_check",
            D("12.74"),
            ARM_AT,
        )


async def test_the_stream_fires_each_signal_once(journal: Journal) -> None:
    clock = SimClock(ARM_AT)
    await save_signals(journal, [sig("XYZ", 1)])
    eng = engine(journal, clock)
    await eng.arm(await load_signals(journal, DAY), {})
    assert await eng.on_trade(trade("XYZ", "12.71")) is None  # below the trigger
    assert await eng.on_trade(trade("XYZ", "12.80", cond=("I",))) is None  # odd lot
    assert await eng.on_trade(trade("XYZ", "12.80", ts=OR_END - timedelta(microseconds=1))) is None
    assert await eng.on_trade(trade("ABC", "99.00")) is None  # not armed
    await clock.advance(timedelta(seconds=10))
    ev = await eng.on_trade(trade("XYZ", "12.72", ts=ARM_AT + timedelta(seconds=9), size=300))
    assert ev is not None
    assert (ev.source, ev.price, ev.size, ev.received_at, ev.rank) == (
        "stream_trade", D("12.72"), 300, ARM_AT + timedelta(seconds=10), 1,
    )  # fmt: skip
    assert await eng.on_trade(trade("XYZ", "12.73")) is None  # one-shot
    assert await statuses(journal) == {"XYZ": ("triggered", True, True)}
    assert [e.signal_id for e in drain(eng.queues["A"])] == ["20261013-XYZ-orb5"]
    assert [e.signal_id for e in drain(eng.queues["B"])] == ["20261013-XYZ-orb5"]


async def test_nothing_triggers_from_last_entry_on(journal: Journal) -> None:
    clock = SimClock(LAST - timedelta(microseconds=1))
    await save_signals(journal, [sig("AAA", 1), sig("BBB", 2)])
    eng = engine(journal, clock)
    await eng.arm(await load_signals(journal, DAY), {})
    assert await eng.on_trade(trade("AAA", "12.72")) is not None
    await clock.advance(timedelta(microseconds=1))
    assert await eng.on_trade(trade("BBB", "12.72")) is None
    expired = await eng.expire()
    assert [s.symbol for s in expired] == ["BBB"]
    assert (await statuses(journal))["BBB"] == ("expired", True, False)
    assert (await statuses(journal))["AAA"][0] == "triggered"
    assert eng.armed_symbols == []


async def test_run_consumes_a_hub_queue(journal: Journal) -> None:
    clock = SimClock(ARM_AT)
    await save_signals(journal, [sig("XYZ", 1)])
    eng = engine(journal, clock)
    await eng.arm(await load_signals(journal, DAY), {})
    hub: asyncio.Queue[object] = asyncio.Queue()
    task = asyncio.create_task(eng.run(hub))  # type: ignore[arg-type]
    hub.put_nowait(Quote(symbol="XYZ", ts=ARM_AT, bid=D("12.70"), bid_size=1, ask=D("12.73"),
                         ask_size=1))  # fmt: skip
    hub.put_nowait(trade("XYZ", "12.75"))
    await hub.join()
    task.cancel()
    assert [e.price for e in drain(eng.queues["A"])] == [D("12.75")]


async def test_a_failed_write_is_skipped_and_the_next_trade_triggers(journal: Journal) -> None:
    clock = SimClock(ARM_AT)
    await save_signals(journal, [sig("XYZ", 1)])
    eng = engine(journal, clock)
    await eng.arm(await load_signals(journal, DAY), {})
    write, failures = journal.write, []

    async def locked_once(fn: Any) -> Any:  # as a database another program holds would
        if not failures:
            failures.append(fn)
            raise sqlite3.OperationalError("database is locked (test)")
        return await write(fn)

    journal.write = locked_once  # type: ignore[method-assign]
    hub: asyncio.Queue[object] = asyncio.Queue()
    task = asyncio.create_task(eng.run(hub))  # type: ignore[arg-type]
    hub.put_nowait(trade("XYZ", "12.75"))
    hub.put_nowait(trade("XYZ", "12.76"))
    await asyncio.wait_for(hub.join(), timeout=5)  # a dead engine never drains the queue
    assert not task.done()  # the engine lives on (D-051)
    task.cancel()
    assert len(failures) == 1
    assert [e.price for e in drain(eng.queues["A"])] == [D("12.76")]  # once, on the next trade
    assert (await statuses(journal))["XYZ"][0] == "triggered"


async def test_a_batch_is_served_by_rank_and_books_are_independent() -> None:
    queues: dict[str, asyncio.Queue[TriggerEvent]] = {"A": asyncio.Queue(), "B": asyncio.Queue()}
    for rank in (3, 1, 2):
        ev = TriggerEvent(signal_id=f"s{rank}", symbol=f"S{rank}", rank=rank, ts=ARM_AT,
                          received_at=ARM_AT, price=D("10"), source="stream_trade")  # fmt: skip
        for q in queues.values():
            q.put_nowait(ev)
    # Book B isn't reading (waiting on a verdict in Phase 4): Book A is served regardless.
    assert [ev.rank for ev in await next_batch(queues["A"])] == [1, 2, 3]
    assert queues["B"].qsize() == 3


async def test_restart_rearms_reserves_and_loses(journal: Journal) -> None:
    clock = SimClock(datetime.combine(DAY, time(11, 0), ET))
    first_armed = datetime.combine(DAY, time(9, 35, 6), ET)
    signals = [
        sig("ARMD", 1),  # still armed, no orders: re-armed, and the arm check fires it now
        sig("ORDR", 2),  # still armed but has orders (a crash after submitting): left alone
        sig("UNDC", 3, status="triggered", triggered_at=first_armed),  # B never decided
        sig("DONE", 4, status="triggered", triggered_at=first_armed),  # has orders
    ]
    await save_signals(journal, signals)
    await journal.execute("UPDATE signals SET armed_at = ?", (first_armed.isoformat(),))
    eng = engine(journal, clock)
    plan = await eng.rearm(
        await load_signals(journal, DAY),
        {"ARMD": snap(trade("ARMD", "12.73", ts=clock.now()))},
        with_orders=frozenset({"20261013-ORDR-orb5", "20261013-DONE-orb5"}),
        decided={"20261013-UNDC-orb5": frozenset({"A"})},
    )
    assert [ev.symbol for ev in plan.rearmed.triggered] == ["ARMD"]
    assert [(book, ev.symbol, ev.source) for book, ev in plan.reserved] == [
        ("B", "UNDC", "restart")
    ]
    assert plan.lost == ()
    assert [e.symbol for e in drain(eng.queues["A"])] == ["ARMD"]
    assert [e.symbol for e in drain(eng.queues["B"])] == ["ARMD", "UNDC"]
    rows = {s.symbol: s for s in await load_signals(journal, DAY)}
    assert rows["ARMD"].armed_at == first_armed  # the original arming time is kept
    assert rows["ORDR"].status == "armed"
    late = engine(journal, SimClock(LAST + timedelta(minutes=5)))
    after = await late.rearm(
        await load_signals(journal, DAY), {}, with_orders=frozenset(),
        decided={"20261013-UNDC-orb5": frozenset({"A"})},
    )  # fmt: skip
    lost = [(book, s.symbol) for book, s in after.lost]
    assert ("B", "UNDC") in lost
    assert after.reserved == ()
