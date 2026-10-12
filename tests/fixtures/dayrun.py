"""Run a whole synthetic day through the real orchestrator (P3.5 scenarios; DECISIONS D-048).

The day runs on a SimClock driven by ``simdrive.drive``, with the journal inline and the
synthetic market's REST answers. Each book trades on a SimBroker fed by its own always-on
"exchange" feed, not by the engine's hub: like Alpaca, the broker keeps filling stops while the
engine is down, which is what the restart scenarios need. Nothing touches the network, and the
same inputs always give the same journal.
"""

import asyncio
import contextlib
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.fixtures import nyse
from tests.fixtures.days import SyntheticDay, SyntheticMarket
from tests.fixtures.exec_harness import RISK, SETTINGS
from trader.config import BookName, RiskConfig, Settings
from trader.core.calendar import TradingCalendar, sync_calendar
from trader.core.clock import ET, SimClock
from trader.core.models import Quote, Trade
from trader.data.recorder import REST_FILE, Recorder, broker_file
from trader.data.replay import RecordingMarketData, ReplayHub
from trader.data.stream import Channel, MarketEvent
from trader.data.universe import UniverseRow
from trader.execution.broker import BrokerAdapter
from trader.execution.recording import RecordingBroker
from trader.execution.sim_broker import SimBroker
from trader.memory.journal import Journal
from trader.notify import Priority
from trader.orchestrator.day import Cadence, DayDeps, DayOrchestrator, DayOutcome, DayPaths
from trader.orchestrator.simdrive import drive

D = Decimal
BOOK_CASH = D(1000)
FAST = Cadence(kill_poll_s=5.0, heartbeat_s=60.0, pump_s=30.0, stale_check_s=5.0)
INSERT_UNIVERSE = (
    "INSERT INTO universe_daily (date, symbol, name, exchange, prev_close, atr14, adv14, "
    "or_vol_avg14, eligible, exclusion_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class WatchedHub(ReplayHub):
    """The engine's hub, remembering every subscription change."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.history: list[tuple[str, list[str]]] = []

    async def set_subscriptions(
        self, symbols: Iterable[str], *, channels: Sequence[Channel] | None = None
    ) -> dict[str, list[str]]:
        wanted = sorted(symbols)
        acked = await super().set_subscriptions(wanted, channels=channels)
        self.history.append((self._clock.now().strftime("%H:%M:%S"), wanted))
        return acked


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(
        self, title: str, message: str, *, priority: Priority = "default", tags: Sequence[str] = ()
    ) -> bool:
        self.sent.append((title, message, priority))
        return True


class DayRun:
    def __init__(
        self,
        root: Path,
        day: SyntheticDay,
        *,
        start: time = time(9, 20),
        books: Sequence[BookName] = ("A",),
        cash: Decimal = BOOK_CASH,
        skew: timedelta = timedelta(0),
        sim_class: type[SimBroker] = SimBroker,
        record: bool = True,
        recording: bool = False,
    ) -> None:
        self.root, self.day = root, day
        self.clock = SimClock(datetime.combine(day.day, start, ET))
        self.market = SyntheticMarket(day, self.clock, skew=skew)
        self.events = day.events()
        self.hub = self.new_hub()
        self.sims = {
            b: sim_class(clock=self.clock, book=b, cash=cash, market_open=self._open) for b in books
        }
        self.exchange = ReplayHub(clock=self.clock, events=self.events, symbol_limit=10_000)
        for sim in self.sims.values():
            self.exchange.add_tap(_feeder(sim))
        self.notifier = FakeNotifier()
        self.recorder = Recorder(root / "recordings", day.day) if record or recording else None
        self.recording = recording  # record REST, broker and stream traffic, as run-day does
        self.hooks: list[tuple[datetime, Callable[[], object]]] = []
        self.journal: Journal
        self.calendar: TradingCalendar
        self.orchestrator: DayOrchestrator

    def new_hub(self) -> WatchedHub:
        """The engine's hub; a restarted engine gets a fresh one, as a new process would."""
        self.hub = WatchedHub(clock=self.clock, events=self.events, symbol_limit=30)
        return self.hub

    def _open(self, now: datetime) -> bool:
        if not self.calendar.is_trading_day(now.date()):
            return False
        session = self.calendar.session(now.date())
        return session.open <= now < session.close

    async def open(self) -> "DayRun":
        self.journal = await Journal.open(self.root / "trader.db", inline=True)
        self.calendar, _ = await sync_calendar(
            self.market.calendar, self.journal, nyse.FIRST, nyse.LAST
        )
        rows = self.day.universe_rows()
        await self.journal.write(lambda conn: conn.executemany(INSERT_UNIVERSE, rows))
        self.market.calls.clear()
        if self.recording and self.recorder is not None:
            recorder = self.recorder
            self.hub.add_tap(lambda event: recorder.record_event(event, rx=self.clock.now()))
            recorder.write_universe(
                [
                    UniverseRow(
                        date=r[0],
                        symbol=r[1],
                        name=r[2],
                        exchange=r[3],
                        prev_close=r[4],
                        atr14=r[5],
                        adv14=r[6],
                        or_vol_avg14=r[7],
                        eligible=True,
                        exclusion_reason=None,
                    )
                    for r in rows
                ]
            )
        await self.exchange.set_subscriptions(sorted({e.event.symbol for e in self.events}))
        await self.exchange.start()
        return self

    @property
    def kill_file(self) -> Path:
        return self.root / "runtime" / "KILL"

    def at(self, hhmmss: str, action: Callable[[], object]) -> None:
        """Do ``action`` at that time of the day (a hook outside the engine)."""
        self.hooks.append((datetime.combine(self.day.day, time.fromisoformat(hhmmss), ET), action))

    def deps(
        self,
        *,
        settings: Settings = SETTINGS,
        risk: RiskConfig = RISK,
        cadence: Cadence = FAST,
        **changes: Any,
    ) -> DayDeps:
        deps = DayDeps(
            settings=settings,
            risk=risk,
            clock=self.clock,
            journal=self.journal,
            calendar=self.calendar,
            market=self._market(),
            hub=self.hub,
            brokers=self._brokers(),
            notifier=self.notifier,
            events=(),
            paths=DayPaths(
                kill_file=self.kill_file,
                heartbeat=self.root / "runtime" / "heartbeat.json",
                reports_dir=self.root / "reports",
                backups_dir=self.root / "backups",
            ),
            recorder=self.recorder,
            at_close=tuple(sim.expire_day_orders for sim in self.sims.values()),
            boot_info={"test": True},
            cadence=cadence,
        )
        return DayDeps(**{**deps.__dict__, **changes})

    def _market(self) -> Any:
        if not (self.recording and self.recorder is not None):
            return self.market
        recorder = self.recorder
        return RecordingMarketData(
            self.market,
            self.clock,  # type: ignore[arg-type]
            lambda record: recorder.append(REST_FILE, record),
        )

    def _brokers(self) -> dict[BookName, BrokerAdapter]:
        if not (self.recording and self.recorder is not None):
            return dict(self.sims)
        recorder = self.recorder
        return {
            book: RecordingBroker(sim, self.clock, _sink(recorder, broker_file(book)))
            for book, sim in self.sims.items()
        }

    async def run(
        self, deps: DayDeps | None = None, *, until: str | None = None
    ) -> DayOutcome | None:
        """Run the engine to the end of the day, or crash it at ``until`` (HH:MM:SS)."""
        self.orchestrator = DayOrchestrator(deps or self.deps())
        crash = (
            None if until is None else datetime.combine(self.day.day, time.fromisoformat(until), ET)
        )
        return await drive(
            self.clock,
            self._job(self.orchestrator, crash),
            watched=[self.journal, *self.sims.values()],
        )

    async def _job(self, orch: DayOrchestrator, crash: datetime | None) -> DayOutcome | None:
        hooks = [asyncio.create_task(self._hook(when, action)) for when, action in self.hooks]
        self.hooks = []
        engine = asyncio.ensure_future(orch.run())
        try:
            if crash is None:
                return await engine
            await self.clock.sleep_until(crash)
            engine.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await engine
            return None
        finally:
            for task in hooks:
                task.cancel()

    async def _hook(self, when: datetime, action: Callable[[], object]) -> None:
        await self.clock.sleep_until(when)
        result = action()
        if asyncio.iscoroutine(result):
            await result

    async def wait_until(self, hhmmss: str) -> None:
        """Let the market (and the brokers) run on while no engine is up."""
        when = datetime.combine(self.day.day, time.fromisoformat(hhmmss), ET)
        # Not the brokers: with no engine listening, their updates wait for the next connection.
        await drive(self.clock, self.clock.sleep_until(when), watched=[self.journal])

    async def close(self) -> None:
        await self.exchange.stop()
        await self.journal.aclose()

    # ---------------------------------------------------------------- journal views

    async def rows(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return await self.journal.fetch_all(sql, params)

    async def statuses(self) -> dict[str, str]:
        rows = await self.rows("SELECT symbol, status FROM signals ORDER BY rel_vol_rank")
        return {r["symbol"]: r["status"] for r in rows}

    async def decisions(self, book: BookName = "A") -> dict[str, str]:
        rows = await self.rows(
            "SELECT s.symbol, d.reason_code FROM final_decisions d JOIN signals s "
            "ON s.id = d.signal_id WHERE d.book = ? ORDER BY s.rel_vol_rank",
            (book,),
        )
        return {r["symbol"]: r["reason_code"] for r in rows}

    async def trades(self, book: BookName = "A") -> dict[str, tuple[Any, ...]]:
        rows = await self.rows(
            "SELECT symbol, qty, entry_price, exit_price, exit_reason FROM trades WHERE book = ? "
            "ORDER BY entry_ts",
            (book,),
        )
        return {
            r["symbol"]: (r["qty"], r["entry_price"], r["exit_price"], r["exit_reason"])
            for r in rows
        }

    async def incidents(self) -> list[tuple[str, str]]:
        rows = await self.rows("SELECT severity, code FROM incidents ORDER BY id")
        return [(r["severity"], r["code"]) for r in rows]

    async def halts(self, book: BookName = "A") -> list[str]:
        rows = await self.rows(
            "SELECT reason FROM halts WHERE book = ? AND cleared_at IS NULL ORDER BY id", (book,)
        )
        return [r["reason"] for r in rows]

    async def states(self) -> list[str]:
        rows = await self.rows("SELECT name FROM day_runs WHERE kind = 'state' ORDER BY id")
        return [r["name"] for r in rows]

    async def steps(self) -> list[str]:
        rows = await self.rows("SELECT name FROM day_runs WHERE kind = 'step' ORDER BY id")
        return [r["name"] for r in rows]


def _sink(recorder: Recorder, name: str) -> Callable[[dict[str, Any]], None]:
    def append(record: dict[str, Any]) -> None:
        recorder.append(name, record)

    return append


def _feeder(sim: SimBroker) -> Callable[[MarketEvent], None]:
    def feed(event: MarketEvent) -> None:
        if isinstance(event, (Trade, Quote)):
            sim.feed(event)

    return feed
