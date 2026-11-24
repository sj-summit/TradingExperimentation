"""The watchdog (P6.2a; RISK.md §6; the Phase 6 plan's decisions 11-14; DECISIONS D-082).

The rules are pure (``phase``, ``coverage``, ``assess``) and tested cell by cell. ``Watchdog.run``
is tested on SimBroker with a SimClock that jumps to each of its waits, and once beside a live
ExecutionService on the same broker and journal."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.exec_harness import CALENDAR, FEES, RISK, SETTINGS, Harness
from tests.fixtures.simtime import run_on_clock
from trader.core.clock import ET, SimClock, fmt_iso
from trader.core.models import OrderIntent, Quote
from trader.execution.broker import Order, OrderRejected, Position
from trader.execution.records import Records
from trader.execution.sim_broker import SimBroker
from trader.memory.journal import Journal
from trader.orchestrator.heartbeat import Beat
from trader.orchestrator.watchdog import (
    BookView,
    RunReport,
    Watchdog,
    WatchdogDeps,
    assess,
    coverage,
    phase,
)

D = Decimal
DAY = date(2026, 10, 9)  # a Friday
SESSION = CALENDAR.session(DAY)
FLATTEN_AT = datetime(2026, 10, 9, 15, 55, tzinfo=ET)


def at(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), ET)


# ---------------------------------------------------------------- the window


def test_the_window_and_its_phases() -> None:
    assert phase(at(9, 24, 59), SESSION) is None
    assert phase(at(9, 25), SESSION) == "before_open"
    assert phase(at(9, 29, 59), SESSION) == "before_open"
    assert phase(at(9, 30), SESSION) == "regular"
    assert phase(at(15, 57, 59), SESSION) == "regular"
    assert phase(at(15, 58), SESSION) == "late"
    assert phase(at(16, 0), SESSION) == "after_close"
    assert phase(at(16, 5), SESSION) == "after_close"
    assert phase(at(16, 5, 1), SESSION) is None


def test_an_early_close_moves_the_window() -> None:
    early = date(2026, 11, 27)
    session = CALENDAR.session(early)
    assert session.close.time() == time(13, 0)
    assert phase(at(12, 57, 59, early), session) == "regular"
    assert phase(at(12, 58, day=early), session) == "late"
    assert phase(at(13, 5, day=early), session) == "after_close"
    assert phase(at(13, 6, day=early), session) is None


# ---------------------------------------------------------------- coverage


def order(oid: str, side: str, kind: str, qty: int, status: str, **extra: Any) -> Order:
    return Order(
        id=oid,
        client_order_id=f"c-{oid}",
        symbol=extra.pop("symbol", "XYZ"),
        side=side,  # type: ignore[arg-type]
        type=kind,
        order_class=extra.pop("order_class", "simple"),
        time_in_force=extra.pop("tif", "day"),
        qty=qty,
        filled_qty=extra.pop("filled", 0),
        status=status,
        **extra,
    )


def position(qty: int, symbol: str = "XYZ") -> Position:
    return Position(symbol=symbol, qty=qty, qty_available=qty, avg_entry_price=D("12.35"))


@pytest.mark.parametrize(
    ("orders", "uncovered", "no_gtc"),
    [
        ([], {"XYZ": 40}, {"XYZ": 40}),
        ([order("s", "sell", "stop", 40, "new")], {}, {"XYZ": 40}),
        ([order("s", "sell", "stop", 40, "accepted", tif="gtc")], {}, {}),
        ([order("s", "sell", "stop", 25, "new")], {"XYZ": 15}, {"XYZ": 40}),
        ([order("m", "sell", "market", 40, "new")], {}, {"XYZ": 40}),  # the exit at work
        ([order("m", "sell", "market", 40, "pending_cancel")], {"XYZ": 40}, {"XYZ": 40}),
        (  # a held leg whose entry is still open (a partial fill) protects nothing yet
            [
                order("e", "buy", "limit", 60, "partially_filled", filled=40, group_id="g"),
                order("s", "sell", "stop", 60, "held", group_id="g"),
            ],
            {"XYZ": 40},
            {"XYZ": 40},
        ),
        ([order("s", "sell", "stop", 40, "held", group_id="g")], {}, {"XYZ": 40}),  # entry final
        ([order("s", "sell", "stop", 40, "new", symbol="ABC")], {"XYZ": 40}, {"XYZ": 40}),
    ],
    ids=[
        "no orders",
        "a working day stop",
        "a working GTC stop",
        "a stop for part",
        "a market sell at work",
        "a sell being cancelled",
        "a held leg, entry open",
        "a held leg, entry final",
        "another symbol's stop",
    ],
)
def test_coverage_is_the_engines_rule(
    orders: list[Order], uncovered: dict[str, int], no_gtc: dict[str, int]
) -> None:
    held, bare, overnight = coverage([position(40), position(-5, "SHORT")], orders)
    assert held == {"XYZ": 40}  # a short is never ours
    assert (bare, overnight) == (uncovered, no_gtc)


# ---------------------------------------------------------------- the rules


STALE = Beat("stale", age_s=95.0, ts=at(9, 58))
ABSENT = Beat("absent")
FRESH = Beat("fresh", age_s=4.0, ts=at(10, 0), books={"A": {}, "B": {}})


def view(
    book: str = "A",
    *,
    held: int = 0,
    bare: int = 0,
    no_gtc: int = 0,
    orders: int = 0,
    **flags: bool,
) -> BookView:
    return BookView(
        book=book,  # type: ignore[arg-type]
        held={"XYZ": held} if held else {},
        uncovered={"XYZ": bare} if bare else {},
        no_gtc={"XYZ": no_gtc} if no_gtc else {},
        orders=orders,
        **flags,
    )


def rules(ph: str, beat: Beat, views: Sequence[BookView], **kw: Any) -> list[tuple[Any, ...]]:
    found = assess(
        ph,
        beat,
        views,
        kill=kw.pop("kill", False),  # type: ignore[arg-type]
        now=kw.pop("now", at(10, 0)),
        flatten_at=FLATTEN_AT,
        episode="E",
        **kw,
    )
    return [(a.kind, a.book, a.severity, a.code, a.halt, a.key) for a in found]


def test_stale_in_the_session_flattens_every_open_book() -> None:
    views = [view("A", held=40, orders=1), view("B", orders=2)]
    assert rules("regular", STALE, views) == [
        ("flatten", "A", "P1", "engine_down_flattened", "p1_incident", "engine_down_flattened:A:E"),
        ("flatten", "B", "P1", "engine_down_flattened", "p1_incident", "engine_down_flattened:B:E"),
    ]
    assert rules("late", ABSENT, views) == rules("regular", STALE, views)
    (a, b) = assess(
        "regular", STALE, views, kill=False, now=at(10, 0), flatten_at=FLATTEN_AT, episode="E"
    )
    assert a.message == "the engine's heartbeat is stale (95 s old): Book A flattened (XYZ 40)"
    assert b.message.endswith("Book B flattened (open orders only)")


def test_stale_with_nothing_open_is_a_p2() -> None:
    assert rules("regular", ABSENT, [view("A"), view("B")]) == [
        ("alert", None, "P2", "engine_down", "p1_incident", "engine_down:E"),
    ]


def test_stale_before_the_open() -> None:
    assert rules("before_open", STALE, [view("A", held=40, bare=40), view("B")]) == [
        ("alert", None, "P2", "engine_down", "p1_incident", "engine_down:E"),
        ("flatten", "A", "P1", "watchdog_unprotected", "p1_incident", "watchdog_unprotected:A"),
    ]


def test_stale_after_the_close() -> None:
    covered = view("A", held=40)  # a GTC stop covers it
    assert rules("after_close", STALE, [covered, view("B", held=10, no_gtc=10)]) == [
        ("flatten", "B", "P1", "watchdog_overnight", "p1_incident", "watchdog_overnight:B"),
    ]
    assert rules("after_close", STALE, [covered]) == [
        ("alert", None, "P2", "engine_down", "p1_incident", "engine_down:E"),
    ]


def test_the_kill_file_with_the_engine_down() -> None:
    views = [view("A", held=40), view("B")]
    assert rules("regular", STALE, views, kill=True) == [
        ("flatten", "A", "P2", "watchdog_kill_file", "kill_switch", "watchdog_kill_file:A:E"),
    ]
    assert rules("regular", STALE, [view("A"), view("B")], kill=True) == [
        ("alert", None, "P2", "engine_down", "p1_incident", "engine_down:E"),
    ]
    assert rules("regular", FRESH, views, kill=True) == []  # the engine handles its KILL file


def test_the_kill_file_in_the_last_two_minutes() -> None:
    assert rules("late", STALE, [view("A", held=40)], kill=True) == [
        ("flatten", "A", "P2", "watchdog_kill_file", "kill_switch", "watchdog_kill_file:A:E"),
    ]


def test_fresh_after_the_close_only_books_no_engine_runs_get_stops() -> None:
    """The engine's after-close pass protects its own books; nobody else's (D-083)."""
    mine = view("A", held=40, bare=40, no_gtc=40, managed=True)
    assert rules("after_close", FRESH, [mine]) == []
    idle = view("B", held=10, bare=10, no_gtc=10)
    assert rules("after_close", FRESH, [mine, idle]) == [
        ("flatten", "B", "P1", "watchdog_overnight", "p1_incident", "watchdog_overnight:B"),
    ]


def test_fresh_and_protected_is_quiet() -> None:
    for ph in ("before_open", "regular", "after_close"):
        assert rules(ph, FRESH, [view("A", held=40), view("B")]) == []


def test_bare_shares_in_a_book_the_engine_runs_get_a_wait_first() -> None:
    views = [view("A", held=40, bare=40, managed=True)]
    assert rules("regular", FRESH, views) == [("recheck", "A", None, "", "p1_incident", "")]
    assert rules("regular", FRESH, views, rechecked=frozenset({"A"})) == [
        ("flatten", "A", "P1", "watchdog_unprotected", "p1_incident", "watchdog_unprotected:A"),
    ]
    assert rules("before_open", FRESH, views) == [("recheck", "A", None, "", "p1_incident", "")]


def test_bare_shares_in_a_book_no_engine_watches_are_read_twice_then_flattened() -> None:
    """No engine wait, but a second read: a stop filling between two reads looks bare (D-083)."""
    views = [view("B", held=10, bare=10)]
    assert rules("regular", FRESH, views) == [("recheck", "B", None, "", "p1_incident", "")]
    found = assess(
        "regular",
        FRESH,
        views,
        kill=False,
        now=at(10, 0),
        flatten_at=FLATTEN_AT,
        episode="E",
        rechecked=frozenset({"B"}),
    )
    assert [(a.kind, a.code) for a in found] == [("flatten", "watchdog_unprotected")]
    assert "a book no engine watches" in found[0].message


def test_the_p2_names_books_it_could_not_read() -> None:
    found = assess(
        "regular",
        STALE,
        [view("B")],
        kill=False,
        now=at(10, 0),
        flatten_at=FLATTEN_AT,
        episode="E",
        blind=["A"],
    )
    assert found[0].message.endswith("nothing is open (Book A couldn't be read)")


@pytest.mark.parametrize(
    ("flags", "now"),
    [
        ({"flattening": True}, at(10, 0)),
        ({"marker": True}, at(10, 0)),
        ({}, FLATTEN_AT - timedelta(seconds=40)),  # the engine's own flatten is near
    ],
    ids=["the engine is flattening", "someone else's marker", "near the flatten time"],
)
def test_the_stop_check_stands_aside(flags: dict[str, bool], now: datetime) -> None:
    views = [view("A", held=40, bare=40, managed=True, **flags)]
    assert rules("regular", FRESH, views, now=now) == []


def test_the_stop_check_runs_until_40_s_before_the_flatten() -> None:
    views = [view("A", held=40, bare=40, managed=True)]
    now = FLATTEN_AT - timedelta(seconds=41)
    assert rules("regular", FRESH, views, now=now) == [
        ("recheck", "A", None, "", "p1_incident", "")
    ]


def test_the_late_check_flattens_whatever_is_held() -> None:
    views = [view("A", held=40, managed=True, flattening=True, marker=True), view("B")]
    expected = [("flatten", "A", "P1", "watchdog_late", "p1_incident", "watchdog_late:A")]
    assert rules("late", FRESH, views) == expected
    assert rules("late", FRESH, [view("A"), view("B")]) == []


# ---------------------------------------------------------------- runs on SimBroker


class Pushed:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, title: str, message: str, **_kw: Any) -> bool:
        self.sent.append(title)
        return True


class World:
    """A watchdog's inputs: two SimBroker accounts, a journal, a heartbeat file, a clock."""

    def __init__(self, root: Path, journal: Journal, clock: SimClock) -> None:
        self.root, self.journal, self.clock = root, journal, clock
        self.brokers = {
            "A": SimBroker(clock=clock, book="A"),
            "B": SimBroker(clock=clock, book="B"),
        }
        self.records = {
            b: Records(journal, book=b, calendar=CALENDAR, fees=FEES, clock=clock)  # type: ignore[arg-type]
            for b in ("A", "B")
        }
        self.pushed = Pushed()
        self.heartbeat = root / "heartbeat.json"
        self.status = root / "watchdog.json"
        self.kill = root / "KILL"

    def beat(self, ts: datetime, books: Sequence[str] = ("A", "B"), **state: Any) -> None:
        self.heartbeat.write_text(
            json.dumps(
                {
                    "ts": fmt_iso(ts),
                    "state": "TRADING",
                    "pid": 4242,
                    "books": {
                        b: {"halted": False, "halts": [], "positions": {}, **state} for b in books
                    },
                }
            ),
            encoding="utf-8",
        )

    async def hold(self, book: str = "A", symbol: str = "XYZ", qty: int = 40) -> Order:
        """A filled OTO entry with its stop leg working, journaled as the engine would."""
        sim = self.brokers[book]
        sim.feed(
            Quote(
                symbol=symbol,
                ts=self.clock.now(),
                bid=D("12.30"),
                bid_size=5,
                ask=D("12.32"),
                ask_size=5,
            )
        )
        accepted = await sim.submit_entry_oto(
            OrderIntent(
                book=book,
                signal_id=f"20261009-{symbol}-orb5",
                symbol=symbol,
                qty=qty,
                entry_limit=D("12.35"),
                stop_price=D("12.21"),
                client_order_id=f"{book}-20261009-{symbol}-entry-1",
                day_size_multiplier=D("1"),
            )  # type: ignore[arg-type]
        )
        await self.records[book].apply(accepted, signal_id=f"20261009-{symbol}-orb5")
        filled = await sim.get_order(accepted.id)
        await self.records[book].apply(filled)
        return filled

    async def bare(self, book: str = "A") -> None:
        """A held position whose stop leg was cancelled: nothing protects it."""
        entry = await self.hold(book)
        leg = next(
            o for o in await self.brokers[book].open_orders() if o.group_id == entry.group_id
        )
        await self.brokers[book].cancel(leg.id)

    def watchdog(self, **changes: Any) -> Watchdog:
        deps = WatchdogDeps(
            clock=self.clock,
            journal=self.journal,
            calendar=CALENDAR,
            brokers=self.brokers,  # type: ignore[arg-type]
            notifier=self.pushed,
            settings=SETTINGS,
            risk=RISK,
            heartbeat=self.heartbeat,
            status=self.status,
            kill_file=self.kill,
            pause=lambda _s: None,
        )
        return Watchdog(WatchdogDeps(**{**deps.__dict__, **changes}))

    async def run(self, **changes: Any) -> RunReport:
        return await run_on_clock(self.clock, self.watchdog(**changes).run())

    async def incidents(self) -> list[tuple[str, str, str]]:
        rows = await self.journal.fetch_all(
            "SELECT severity, component, code FROM incidents ORDER BY id"
        )
        return [(r["severity"], r["component"], r["code"]) for r in rows]

    async def halts(self, book: str) -> list[str]:
        rows = await self.journal.fetch_all(
            "SELECT reason FROM halts WHERE book = ? AND cleared_at IS NULL ORDER BY id", (book,)
        )
        return [r["reason"] for r in rows]

    async def held(self, book: str = "A") -> dict[str, int]:
        return {p.symbol: p.qty for p in await self.brokers[book].positions()}


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    # inline: reads and writes run on the loop, so run_on_clock can't move time while a
    # coroutine beside the watchdog is still writing (D-047)
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    yield World(tmp_path, journal, SimClock(at(10, 0)))
    await journal.aclose()


async def test_outside_the_window_and_on_a_holiday_nothing_is_read(tmp_path: Path) -> None:
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    try:
        early = World(tmp_path, journal, SimClock(at(9, 0)))
        assert (await early.run()).verdict == "outside the window"
        holiday = World(tmp_path, journal, SimClock(datetime(2026, 10, 10, 10, 0, tzinfo=ET)))
        assert (await holiday.run()).verdict == "not a trading day"
        assert early.brokers["A"].submits == []
        assert await early.incidents() == []
        status = json.loads(early.status.read_text(encoding="utf-8"))
        assert status["verdict"] == "not a trading day"
    finally:
        await journal.aclose()


async def test_a_dead_engines_books_are_flattened_and_alerted(world: World) -> None:
    await world.hold("A")
    world.beat(at(9, 58))  # two minutes old
    report = await world.run()
    assert report.verdict == "acted"
    assert await world.held("A") == {}
    assert await world.halts("A") == ["p1_incident"]  # the flattening marker is cleared
    assert await world.halts("B") == []
    assert await world.incidents() == [("P1", "watchdog", "engine_down_flattened")]
    assert world.pushed.sent == ["P1 engine_down_flattened"]  # the engine's pump is dead
    rows = await world.journal.fetch_all(
        "SELECT purpose, status FROM orders WHERE side = 'sell' AND purpose = 'incident'"
    )
    assert [(r["purpose"], r["status"]) for r in rows] == [("incident", "filled")]
    fills = await world.journal.fetch_all("SELECT COUNT(*) AS n FROM fills WHERE side = 'sell'")
    assert fills[0]["n"] == 1  # booked once
    status = json.loads(world.status.read_text(encoding="utf-8"))
    assert (status["verdict"], status["results"]) == ("acted", {"A": "flat"})
    assert status["heartbeat"]["status"] == "stale"


async def test_the_alert_says_once_per_episode(world: World) -> None:
    world.beat(at(9, 0))  # stale, nothing open
    await world.run()
    await world.run()  # the next minute: the same episode
    assert await world.incidents() == [("P2", "watchdog", "engine_down")]
    world.beat(at(10, 30))  # a restarted engine beat, then died again: a new episode
    await world.clock.advance_to(at(10, 40))
    await world.run()
    assert await world.incidents() == [("P2", "watchdog", "engine_down")] * 2


async def test_a_p1_for_the_episode_stops_its_p2(world: World) -> None:
    await world.hold("A")
    world.beat(at(9, 58))
    await world.run()
    await world.clock.advance_to(world.clock.now() + timedelta(minutes=1))
    await world.run()  # flat now: nothing open, the same dead engine
    assert await world.incidents() == [("P1", "watchdog", "engine_down_flattened")]


async def test_a_heartbeat_that_comes_back_within_25_s_is_left_alone(world: World) -> None:
    await world.hold("A")
    world.beat(at(9, 58))

    async def engine_beats_again() -> None:
        await world.clock.sleep_until(at(10, 0, 10))
        world.beat(at(10, 0, 10))

    report, _ = await run_on_clock(
        world.clock, asyncio.gather(world.watchdog().run(), engine_beats_again())
    )
    assert report.verdict == "ok"
    assert await world.held("A") == {"XYZ": 40}
    assert await world.incidents() == []


async def test_bare_shares_the_engine_doesnt_cover_in_30_s_are_flattened(world: World) -> None:
    await world.bare("A")
    world.beat(at(10, 0))
    started = world.clock.now()
    report = await world.run()
    assert world.clock.now() - started >= timedelta(seconds=30)  # it waited for the engine
    assert await world.held("A") == {}
    assert await world.incidents() == [("P1", "watchdog", "watchdog_unprotected")]
    assert world.pushed.sent == []  # a live engine's pump sends it
    assert report.results == {"A": "flat"}


async def test_bare_shares_the_engine_covers_during_the_wait_are_left(world: World) -> None:
    await world.bare("A")
    world.beat(at(10, 0))

    async def engine_protects() -> None:
        await world.clock.sleep_until(at(10, 0, 5))
        cid = await world.records["A"].pending_sell(
            symbol="XYZ", qty=40, leg="stop", purpose="protective_stop", stop_price=D("12.21")
        )
        await world.records["A"].apply(
            await world.brokers["A"].submit_stop("XYZ", 40, D("12.21"), cid)
        )

    report, _ = await run_on_clock(
        world.clock, asyncio.gather(world.watchdog().run(), engine_protects())
    )
    assert report.verdict == "ok"
    assert await world.held("A") == {"XYZ": 40}
    assert await world.incidents() == []


async def test_an_engine_that_dies_during_the_wait_is_left_to_the_next_run(world: World) -> None:
    """A stale read after the wait gets no action this run: the next run reads it twice."""
    await world.bare("A")
    world.beat(at(10, 0))

    async def engine_dies() -> None:
        await world.clock.sleep_until(at(10, 0, 20))
        world.beat(at(9, 0))

    report, _ = await run_on_clock(
        world.clock, asyncio.gather(world.watchdog().run(), engine_dies())
    )
    assert report.verdict == "ok"
    assert await world.held("A") == {"XYZ": 40}
    assert await world.incidents() == []


async def test_a_book_no_engine_runs_is_flattened_after_a_second_look(world: World) -> None:
    await world.bare("B")
    world.beat(at(10, 0), books=("A",))  # run-day --books A
    started = world.clock.now()
    await world.run()
    assert world.clock.now() - started < timedelta(seconds=30)
    assert await world.held("B") == {}
    assert await world.halts("B") == ["p1_incident"]


async def test_a_book_no_engine_runs_is_left_when_its_shares_go_between_reads(
    world: World,
) -> None:
    await world.bare("B")
    world.beat(at(10, 0), books=("A",))

    async def sold_meanwhile() -> None:
        await world.clock.sleep_until(at(10, 0, 1))
        cid = await world.records["B"].pending_sell(
            symbol="XYZ", qty=40, leg="exit", purpose="manual"
        )
        await world.records["B"].apply(await world.brokers["B"].submit_market_sell("XYZ", 40, cid))

    report, _ = await run_on_clock(
        world.clock, asyncio.gather(world.watchdog().run(), sold_meanwhile())
    )
    assert report.verdict == "ok"
    assert await world.incidents() == []
    assert await world.halts("B") == []


async def test_a_new_incident_after_a_resume_alerts_again(world: World) -> None:
    """One alert per halt: the same halt never alerts twice, a new one does (D-083)."""
    await world.bare("A")
    world.beat(at(10, 0))
    await world.run()
    await world.clock.advance_to(at(10, 5))
    world.beat(at(10, 5))
    await world.run()  # still halted and flat: nothing new
    assert await world.incidents() == [("P1", "watchdog", "watchdog_unprotected")]
    await world.journal.execute(
        "UPDATE halts SET cleared_at = ?, cleared_by = 'trader resume' WHERE book = 'A'",
        (fmt_iso(at(10, 6)),),
    )
    await world.clock.advance_to(at(11, 0))
    world.brokers["A"].hold("XYZ", 5, D("12.30"))  # bare shares again, later that day
    world.beat(at(11, 0))
    await world.run()
    assert await world.incidents() == [("P1", "watchdog", "watchdog_unprotected")] * 2


async def test_a_morning_not_flat_doesnt_silence_the_late_check(world: World) -> None:
    await world.hold("A")
    await world.journal.execute(
        "INSERT INTO incidents (ts, severity, component, code, message) VALUES "
        "(?, 'P1', 'flatten', 'not_flat', 'Book A not flat after trader flatten: XYZ 40')",
        (fmt_iso(at(10, 0)),),
    )
    await world.clock.advance_to(at(15, 58, 30))
    world.beat(at(15, 58, 25))
    await world.run()
    assert await world.incidents() == [
        ("P1", "flatten", "not_flat"),
        ("P1", "watchdog", "watchdog_late"),
    ]


async def test_a_p1_after_the_close_stops_that_episodes_p2(world: World) -> None:
    await world.hold("A")
    await world.clock.advance_to(at(16, 1))
    world.beat(at(15, 59))  # the engine died at 15:59
    await world.run()  # GTC stops, a P1
    await world.clock.advance_to(at(16, 2))
    await world.run()  # covered now: nothing for a P1 to say, and the P2 stays quiet
    assert await world.incidents() == [("P1", "watchdog", "watchdog_overnight")]


async def test_a_book_that_wont_go_flat_is_tried_each_run_but_alerts_once(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One alert per halt: a flatten that keeps failing is tried again each run, quietly."""
    await world.hold("A")

    async def refused(*_args: Any) -> Order:
        raise OrderRejected("the sell is refused")

    monkeypatch.setattr(world.brokers["A"], "submit_market_sell", refused)
    world.beat(at(9, 0))  # the engine died before the open
    first = await world.run()
    await world.clock.advance_to(at(10, 5))
    second = await world.run()
    assert (first.verdict, second.verdict) == ("acted", "acted")
    assert await world.incidents() == [("P1", "watchdog", "engine_down_flattened")]
    assert await world.halts("A") == ["p1_incident"]
    assert [p.symbol for p in await world.brokers["A"].positions()] == ["XYZ"]


async def test_the_kill_file_sells_with_its_own_purpose(world: World) -> None:
    await world.hold("A")
    world.beat(at(9, 0))
    world.kill.write_text("", encoding="utf-8")
    await world.run()
    rows = await world.journal.fetch_all(
        "SELECT purpose FROM orders WHERE side = 'sell' AND type = 'market'"
    )
    assert [r["purpose"] for r in rows] == ["kill_switch"]


async def test_the_late_check(world: World) -> None:
    await world.hold("A")
    await world.clock.advance_to(at(15, 58, 30))
    world.beat(at(15, 58, 25), flattening=True)
    await world.run()
    assert await world.held("A") == {}
    assert await world.incidents() == [("P1", "watchdog", "watchdog_late")]


async def test_the_late_check_leaves_the_p1_to_the_engine_when_it_raised_one(
    world: World,
) -> None:
    await world.hold("A")
    await world.clock.advance_to(at(15, 58, 30))
    world.beat(at(15, 58, 25))
    await world.journal.execute(
        "INSERT INTO incidents (ts, severity, component, code, message) VALUES "
        "(?, 'P1', 'orchestrator', 'not_flat', 'Book A: held XYZ 40')",
        (fmt_iso(at(15, 58, 1)),),
    )
    await world.run()
    assert await world.held("A") == {}
    assert await world.incidents() == [("P1", "orchestrator", "not_flat")]


async def test_after_the_close_a_dead_engines_shares_get_gtc_stops(world: World) -> None:
    await world.hold("A")  # its stop leg is a day order: it expires at the close
    await world.clock.advance_to(at(16, 2))
    world.beat(at(15, 50))
    await world.run()
    assert await world.held("A") == {"XYZ": 40}  # nothing can be sold after the close
    gtc = [o for o in await world.brokers["A"].open_orders() if o.time_in_force == "gtc"]
    assert [(o.side, o.type, o.qty, o.stop_price) for o in gtc] == [
        ("sell", "stop", 40, D("12.21"))
    ]
    assert await world.incidents() == [("P1", "watchdog", "watchdog_overnight")]


async def test_the_kill_file_flattens_a_dead_engines_books(world: World) -> None:
    await world.hold("A")
    world.beat(at(9, 0))
    world.kill.write_text("", encoding="utf-8")
    await world.run()
    assert await world.held("A") == {}
    assert await world.halts("A") == ["kill_switch"]
    assert await world.incidents() == [("P2", "watchdog", "watchdog_kill_file")]


async def test_a_broker_that_cant_be_read_is_a_p2_a_day(world: World) -> None:
    async def broken() -> list[Position]:
        raise ConnectionError("down")

    world.brokers["B"].positions = broken  # type: ignore[method-assign]
    await world.hold("A")
    world.beat(at(9, 58))
    await world.run()
    await world.run()
    assert await world.held("A") == {}  # Book A is still handled
    assert await world.incidents() == [
        ("P2", "watchdog", "watchdog_blind"),
        ("P1", "watchdog", "engine_down_flattened"),
    ]


async def test_a_sell_a_killed_flatten_left_submitting_is_settled(world: World) -> None:
    await world.hold("A")
    stuck = await world.records["A"].pending_sell(
        symbol="XYZ", qty=40, leg="exit", purpose="incident"
    )
    await world.clock.advance_to(at(10, 1))  # well past its submit
    world.beat(at(9, 58))
    await world.run()
    row = await world.records["A"].order(stuck)
    assert row is not None
    assert row.status == "lost"
    assert await world.held("A") == {}


# ---------------------------------------------------------------- beside a live engine


async def test_flattening_beside_a_live_engine(tmp_path: Path) -> None:
    """The engine runs Book A and holds XYZ; its heartbeat goes stale (a hung loop): the watchdog
    flattens on the same broker and journal while the service keeps running. The marker stands
    the engine down, its stream recognizes the watchdog's sells, and reconcile finds no stranger."""
    h = await Harness(tmp_path).open()
    try:
        h.quote("12.30", "12.32")
        assert (await h.enter()).outcome == "submitted"
        await h.advance(2)
        assert await h.held() == {"XYZ": 40}
        world = World(tmp_path, h.journal, h.clock)
        world.brokers = {"A": h.sim}  # type: ignore[assignment]
        world.beat(h.clock.now() - timedelta(minutes=3))
        task = asyncio.ensure_future(world.watchdog().run())
        for _ in range(400):
            if task.done():
                break
            await h.advance(1)
        report = task.result()
        assert report.results == {"A": "flat"}
        assert await h.held() == {}
        await h.advance(90)  # the engine's monitor and its reconcile run on
        assert await h.working_stops() == []  # it placed no stop after the marker
        assert ("P1", "reconcile_mismatch") not in await h.incidents()
        assert ("P1", "unprotected_position") not in await h.incidents()
        sells = await h.journal.fetch_all(
            "SELECT status FROM orders WHERE side = 'sell' AND type = 'market'"
        )
        assert [r["status"] for r in sells] == ["filled"]
    finally:
        await h.close()


async def test_the_engine_and_the_watchdog_flattening_at_once(tmp_path: Path) -> None:
    h = await Harness(tmp_path).open()
    try:
        h.quote("12.30", "12.32")
        await h.enter()
        await h.advance(2)
        world = World(tmp_path, h.journal, h.clock)
        world.brokers = {"A": h.sim}  # type: ignore[assignment]
        world.beat(h.clock.now() - timedelta(minutes=3))
        task = asyncio.ensure_future(world.watchdog().run())
        await h.advance(25)  # the watchdog reads the heartbeat again, then flattens...
        engine = asyncio.ensure_future(h.service.flatten(purpose="manual"))  # ...and so does it
        for _ in range(400):
            if task.done() and engine.done():
                break
            await h.advance(1)
        assert engine.result().flat
        assert await h.held() == {}
        rows = await h.journal.fetch_all(
            "SELECT status FROM orders WHERE side = 'sell' AND type = 'market'"
        )
        statuses = sorted(r["status"] for r in rows)
        assert statuses.count("filled") == 1  # one sale; a second is refused, never doubled
        assert len(statuses) <= 2
        entries = await h.journal.fetch_all(
            "SELECT kind FROM ledger_entries WHERE book = 'A' AND kind = 'sell'"
        )
        assert len(entries) == 1
    finally:
        await h.close()


# ---------------------------------------------------------------- the command


class FakeRest:
    async def calendar(self, start: date, end: date) -> list[dict[str, str]]:
        from tests.fixtures import nyse

        return nyse.calendar_rows(max(start, nyse.FIRST), min(end, nyse.LAST))

    async def __aenter__(self) -> "FakeRest":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def offline(repo: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, SimBroker]:
    from tests.fixtures.simtime import JumpClock
    from trader.core import clock as clock_module
    from trader.data import alpaca_rest
    from trader.execution import alpaca_broker

    clock = JumpClock(at(10, 0))
    brokers = {"A": SimBroker(clock=clock, book="A"), "B": SimBroker(clock=clock, book="B")}
    monkeypatch.setattr(clock_module, "RealClock", lambda: clock)
    monkeypatch.setattr(alpaca_rest.AlpacaRest, "from_config", lambda _cfg, _clock: FakeRest())
    monkeypatch.setattr(
        alpaca_broker.AlpacaBroker, "from_config", lambda _cfg, book, _clock: brokers[book]
    )
    return brokers


def test_trader_watchdog(repo: Path, offline: dict[str, SimBroker]) -> None:
    from typer.testing import CliRunner

    from trader.cli import app

    result = CliRunner().invoke(app, ["--root", str(repo), "watchdog"])
    assert result.exit_code == 0, result.output
    assert "acted; heartbeat absent; Book A: flat; Book B: flat" in result.output
    runtime = repo / "runtime"
    assert json.loads((runtime / "watchdog.json").read_text(encoding="utf-8"))["verdict"] == "acted"
    assert (runtime / "logs" / "watchdog-2026-10-09.jsonl").exists()  # its own log
    assert not (runtime / "logs" / "2026-10-09.jsonl").exists()  # never the engine's


def test_one_watchdog_run_at_a_time(repo: Path, offline: dict[str, SimBroker]) -> None:
    from typer.testing import CliRunner

    from trader.cli import app
    from trader.orchestrator.lock import WATCHDOG_LOCK_NAME, RunLock

    lock = RunLock(repo / "runtime" / WATCHDOG_LOCK_NAME)
    lock.acquire()
    try:
        result = CliRunner().invoke(app, ["--root", str(repo), "watchdog"])
    finally:
        lock.release()
    assert result.exit_code == 0
    assert "another watchdog run is still going" in result.output
    assert not (repo / "runtime" / "watchdog.json").exists()


async def test_a_failed_lookup_of_a_stuck_sell_doesnt_stop_the_flatten(world: World) -> None:
    await world.hold("A")
    await world.records["A"].pending_sell(symbol="XYZ", qty=40, leg="exit", purpose="incident")
    await world.clock.advance_to(at(10, 1))

    async def unreachable(_cid: str) -> Order | None:
        raise ConnectionError("lookup failed")

    world.brokers["A"].order_by_client_id = unreachable  # type: ignore[method-assign]
    world.beat(at(9, 58))
    await world.run()
    assert await world.held("A") == {}
