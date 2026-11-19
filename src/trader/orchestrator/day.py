"""The trading day (A§3, A§4.9; DECISIONS D-048) as one explicit state machine.

``BOOT -> PREMARKET -> OR_SCAN -> TRADING -> FLATTEN -> EOD -> REVIEW -> REPORT -> DONE``

- **Journaled:** every transition goes to ``day_runs`` and ``trading_days.run_status``.
- **Clock-driven:** each state waits for its time on the injected clock, so the same code runs
  live, in ``--sim``, in replay and in tests.
- **Fails toward the exits:** a state that fails before the flatten closes the day for entries
  and falls through to FLATTEN and EOD; exits stay open (A§1).
- **Restartable:** a restart runs the machine again. The scan is loaded, not refetched; armed
  signals are re-armed; positions today's journal explains are adopted; finished EOD steps are
  skipped (A§3, resolution 11).
- **Book B** (D-068, D-069), when it runs: its jobs (``llm_jobs.BookBJobs``) boot with the day,
  brief at 09:05 in PREMARKET, vet at arming in OR_SCAN, and review after the shadow job. Its
  worker parks a trigger whose verdict is pending (``Defer``) and asks it again when the store
  changes or at its deadline; at the last entry every parked trigger is settled. Nothing of it
  runs on a Book-A-only day, which stays exactly as it was.
"""

import asyncio
import contextlib
import dataclasses
import json
import sqlite3
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import structlog

from trader.books.base import Book, Defer, decided, record_skips, signals_with_orders
from trader.books.baseline import BaselineBook
from trader.books.llm_filtered import LlmFilteredBook
from trader.books.verdicts import VerdictStore
from trader.config import BookName, RiskConfig, Settings
from trader.core.calendar import TradingCalendar
from trader.core.clock import ET, Clock, fmt_iso
from trader.core.events import Event
from trader.core.models import Asset, Signal, Snapshot, TriggerEvent
from trader.data.recorder import BOOT_FILE, Recorder
from trader.data.stream import MarketEvent
from trader.execution.broker import BrokerAdapter, BrokerError
from trader.execution.reconcile import Reconciler
from trader.execution.records import Records, read_book_day
from trader.execution.service import ExecutionService, FlattenResult, TradingDay
from trader.memory.incidents import Severity, record_incident
from trader.memory.journal import Journal
from trader.memory.regime import RegimeTags, ensure_regime, load_regime, signal_features
from trader.memory.shadow import ShadowParams, compute_shadow
from trader.notify import Notifier, pump_alerts
from trader.orchestrator.capabilities import CURRENT, FEES, SHADOW_TAPE, SKEW_HOURLY
from trader.orchestrator.eod import (
    BookClose,
    EodContext,
    close_book,
    mark_step,
    record_state,
    run_status,
    step_done,
)
from trader.orchestrator.inputs import LiveInputs
from trader.orchestrator.llm_jobs import BookBJobs, LlmDeps
from trader.orchestrator.packs import PackContext
from trader.orchestrator.plan import DayPlan, day_plan
from trader.orchestrator.ports import DayMarket, Hub
from trader.orchestrator.tasks import Heartbeat, StaleWatch, Subscriptions, every
from trader.reports.daily import build_daily, write_daily
from trader.risk.breakers import BookStats, Halts
from trader.risk.gate import RiskGate
from trader.risk.killswitch import KillSwitch
from trader.risk.ledger import LedgerError, SettlementLedger, start_of_day_cash
from trader.strategy.orb_sip import (
    SLOW_SCAN_S,
    OrbSipSelector,
    ScanFlag,
    ScanResult,
    load_signals,
)
from trader.strategy.triggers import TriggerEngine, next_batch

log = structlog.get_logger(__name__)

State = Literal[
    "BOOT", "PREMARKET", "OR_SCAN", "TRADING", "FLATTEN", "EOD", "REVIEW", "REPORT", "DONE"
]
MAX_CLOCK_SKEW_S = 2.0  # RISK.md §9
CLOCK_TRIES = 3  # a reading out of tolerance is taken again; the shortest round trip counts
FLATTEN_PASS = timedelta(seconds=60)
AFTER_CLOSE_TRIES = 6  # the after-close pass, every flatten_retry_s, before a P1 not_flat
EOD_RETRY = timedelta(seconds=30)  # a failed EOD is retried...
EOD_RETRY_WINDOW = timedelta(minutes=7)  # ...until the shadow time, or this long on a rerun
CALENDAR_SLICE = timedelta(days=45)  # boot.json keeps this much calendar around the day
STOP_POLL = timedelta(milliseconds=100)


@dataclass(frozen=True)
class Cadence:
    """How often the background loops run. Tests may slow them; live uses the defaults."""

    kill_poll_s: float = 1.0
    heartbeat_s: float = 10.0
    pump_s: float = 5.0
    stale_check_s: float = 5.0
    flatten_retry_s: float = 5.0
    connect_timeout_s: float = 15.0
    unexplained_flatten_after_open_s: float = 5.0  # A§3: 09:30:05
    skew_check_s: float = 3600.0  # the hourly clock reading (D-055)


@dataclass(frozen=True)
class DayPaths:
    kill_file: Path
    heartbeat: Path
    reports_dir: Path
    backups_dir: Path


@dataclass(frozen=True)
class DayDeps:
    settings: Settings
    risk: RiskConfig
    clock: Clock
    journal: Journal
    calendar: TradingCalendar
    market: DayMarket
    hub: Hub
    brokers: Mapping[BookName, BrokerAdapter]
    notifier: Notifier
    events: tuple[Event, ...]
    paths: DayPaths
    recorder: Recorder | None = None
    ensure_universe: Callable[[date], Awaitable[int]] | None = None  # builds it if missing
    at_close: tuple[Callable[[], None], ...] = ()  # e.g. SimBroker.expire_day_orders
    boot_info: Mapping[str, Any] = field(default_factory=dict)  # config hashes, git SHA
    cadence: Cadence = Cadence()
    capabilities: frozenset[str] = CURRENT  # a replay grants only its recording's (D-055)
    llm: LlmDeps | None = None  # Book B's runner and packs: required when B is among the brokers
    book_b_off: str | None = None  # why Book B isn't running today (--no-llm, ...), for the report


@dataclass(frozen=True)
class DayOutcome:
    day: date
    state: State
    note: str = ""


@dataclass
class _Book:
    name: BookName
    broker: BrokerAdapter
    records: Records
    service: ExecutionService
    policy: Book
    equity_sod: Decimal
    store: VerdictStore | None = None  # Book B's
    worker: "asyncio.Task[None] | None" = None
    busy: bool = False
    not_flat_alerted: bool = False
    # Book B: triggers waiting for their verdict, by signal id, with their deadlines (D-068)
    parked: dict[str, tuple[TriggerEvent, datetime]] = field(default_factory=dict)


class DayOrchestrator:
    def __init__(self, deps: DayDeps) -> None:
        self._d = deps
        self._clock, self._journal = deps.clock, deps.journal
        self._halts = Halts(deps.journal, deps.clock)
        self._stats = BookStats(deps.journal, deps.clock)
        self.state: State = "BOOT"
        self.day = deps.clock.now().date()
        self._started = deps.clock.now()
        self._plan: DayPlan | None = None
        self._books: dict[BookName, _Book] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._signals: dict[str, Signal] = {}
        self._engine: TriggerEngine | None = None
        self._engine_task: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[MarketEvent] | None = None
        self._regime: RegimeTags | None = None
        self._assets: dict[str, Asset] = {}
        self._skew = False
        self._skew_alerted = False
        self._failed: list[State] = []  # states after the flatten that raised (D-051)
        self._inputs: LiveInputs | None = None
        self._jobs: BookBJobs | None = None  # Book B's LLM work, when it runs
        self._heartbeat: Heartbeat | None = None
        self._stale: StaleWatch | None = None
        self._subs = Subscriptions(
            hub=deps.hub,
            journal=deps.journal,
            clock=deps.clock,
            limit=deps.settings.data.stream_symbol_limit,
            regime=deps.settings.data.regime_symbols,
        )

    @property
    def plan(self) -> DayPlan:
        if self._plan is None:
            raise RuntimeError("the day has no plan before BOOT")
        return self._plan

    # ---------------------------------------------------------------- the machine

    async def run(self) -> DayOutcome:
        self._started = self._clock.now()
        if not self._d.calendar.is_trading_day(self.day):
            return DayOutcome(self.day, "DONE", "not a trading day")
        if await run_status(self._journal, self.day) == "DONE":
            return DayOutcome(self.day, "DONE", "this session already ran to the end")
        self._plan = day_plan(self._d.calendar, self.day, self._d.settings, self._d.risk)
        try:
            await self._boot()
            try:
                await self._premarket()
                await self._or_scan()
                await self._trading()
            except Exception:
                log.exception("the day failed before the flatten", state=self.state)
                await self._incident(
                    "P2", "state_failed", f"{self.state} failed: no more entries today"
                )
            finally:
                await self._stop_trading()
            for step in (self._flatten, self._eod, self._review, self._report):
                try:
                    await step()
                except Exception:
                    log.exception("day state failed", state=self.state)
                    await self._incident("P2", "state_failed", f"{self.state} failed")
                    self._failed.append(self.state)
            if self._failed:  # not DONE: run-day again today redoes only the unfinished steps
                await self._backup()
                note = f"{', '.join(self._failed)} failed: run run-day again today to finish"
                return DayOutcome(self.day, self.state, note)
            await self._enter("DONE")
            if self._heartbeat is not None:
                await self._heartbeat.beat()  # the last heartbeat says DONE
            await self._backup()  # after DONE is journaled, so the copy holds the whole day
            return DayOutcome(self.day, "DONE")
        finally:
            await self._shutdown()

    async def _enter(self, state: State) -> None:
        self.state = state
        await record_state(self._journal, self.day, state, self._clock.now())
        log.info("day state", state=state, day=self.day.isoformat())

    # ---------------------------------------------------------------- BOOT

    async def _boot(self) -> None:
        await self._enter("BOOT")
        d, plan = self._d, self.plan
        last_chance = self._clock.now() >= plan.times.flatten  # no re-check before a scan
        self._skew = await self._clock_skewed(final=last_chance)
        if self._clock.now() < plan.times.flatten and d.ensure_universe is not None:
            try:
                eligible = await d.ensure_universe(self.day)
                log.info("universe ready", day=self.day.isoformat(), eligible=eligible)
            except Exception as exc:
                log.exception("universe build failed")
                await self._incident("P2", "universe_unavailable", f"{type(exc).__name__}: {exc}")
        self._regime = await ensure_regime(
            journal=self._journal,
            market=d.market,
            clock=self._clock,
            d=d.calendar.previous_session(self.day),
            symbol=d.settings.data.regime_symbols[0],
        )
        await self._load_assets(final=last_chance)
        self._stale = StaleWatch(
            hub=d.hub,
            clock=self._clock,
            journal=self._journal,
            plan=plan,
            warn_s=d.settings.data.stale_warn_s,
            halt_s=d.risk.data_stale_halt_s,
        )
        stale = self._stale
        self._inputs = LiveInputs(
            hub=d.hub,
            market=d.market,
            brokers=d.brokers,
            halts=self._halts,
            clock=self._clock,
            times=plan.times,
            mode=d.settings.mode,
            risk=d.risk,
            feed=d.settings.data.feed,
            events=d.events,
            assets=self._assets,
            kill_file=d.paths.kill_file,
            hub_stale=lambda: stale.stale,
        )
        if "B" in d.brokers:
            await self._boot_book_b()
        elif d.book_b_off:  # the report, even one regenerated later, says why (D-069)
            await self._journal.execute(
                "INSERT INTO day_runs (date, kind, name, at, detail) "
                "VALUES (?, 'note', 'book_b_off', ?, ?)",
                (self.day.isoformat(), fmt_iso(self._clock.now()), d.book_b_off),
            )
        for name in sorted(d.brokers):
            await self._start_book(name)
        self._heartbeat = Heartbeat(
            d.paths.heartbeat,
            self._clock,
            services={n: b.service for n, b in self._books.items()},
            halts=self._halts,
            state=lambda: self.state,
            hub_age=d.hub.last_event_age_s,
        )
        kill = KillSwitch(
            d.paths.kill_file,
            halts=self._halts,
            clock=self._clock,
            books=sorted(self._books),
            flatten=self._kill_flatten,
            poll_s=d.cadence.kill_poll_s,
        )
        cadence = d.cadence
        self._spawn(every(self._clock, cadence.kill_poll_s, kill.check, name="kill-switch"))
        self._spawn(every(self._clock, cadence.heartbeat_s, self._heartbeat.beat, name="heartbeat"))
        self._spawn(every(self._clock, cadence.pump_s, self._pump, name="alerts"))
        self._spawn(every(self._clock, cadence.stale_check_s, stale.check, name="staleness"))
        if SKEW_HOURLY in d.capabilities:
            self._spawn(self._hourly_skew())
        await d.hub.start()
        if not await d.hub.wait_connected(cadence.connect_timeout_s):
            await self._incident("P2", "stream_unavailable", "market data stream not connected")
        if self._jobs is not None:  # after the stock stream: it takes the first connection
            self._jobs.start_news()
        await self._write_boot_record()

    async def _clock_skewed(self, *, final: bool) -> bool:
        """RISK.md §9: more than 2 s from Alpaca's clock is a P2 and nothing is armed. A clock
        that can't be checked counts as skewed: entries fail closed. Boot's verdict isn't
        ``final``: PREMARKET checks again before the scan (DECISIONS D-051)."""
        then = "nothing armed today" if final else "checking again before the scan"
        try:
            skew = await self._measure_skew()
        except Exception as exc:
            await self._incident(
                "P2", "clock_skew", f"clock not verified: {type(exc).__name__}; {then}"
            )
            self._skew_alerted = True
            return True
        if abs(skew) <= MAX_CLOCK_SKEW_S:
            log.info("clock checked", skew_s=round(skew, 3))
            self._skew_alerted = False
            return False
        await self._incident(
            "P2", "clock_skew", f"system clock is {skew:+.1f} s from Alpaca's: {then}"
        )
        self._skew_alerted = True  # the hourly reading won't repeat it (D-055)
        return True

    async def _hourly_skew(self) -> None:
        """Read the skew every ``skew_check_s`` after boot's reading (D-055): the PC's clock
        drifts tenths of a second over a session, and Windows can step it."""
        period = self._d.cadence.skew_check_s
        await self._clock.sleep_until(self._clock.now() + timedelta(seconds=period))
        await every(self._clock, period, self._read_skew, name="clock-skew")

    async def _read_skew(self) -> None:
        """One reading, journaled in ``day_runs``; past RISK.md §9's 2 s a P2, once per
        episode. It only alerts: boot's check alone decides whether entries are armed."""
        try:
            skew = await self._measure_skew()
        except Exception as exc:
            log.warning("clock skew not read", error=f"{type(exc).__name__}: {exc}")
            return
        await self._journal.execute(
            "INSERT INTO day_runs (date, kind, name, at, detail) VALUES (?, 'clock', 'skew', ?, ?)",
            (self.day.isoformat(), fmt_iso(self._clock.now()), f"{skew:+.3f}"),
        )
        log.info("clock checked", skew_s=round(skew, 3))
        if abs(skew) <= MAX_CLOCK_SKEW_S:
            self._skew_alerted = False
        elif not self._skew_alerted:
            self._skew_alerted = True
            await self._incident(
                "P2", "clock_skew", f"system clock is {skew:+.1f} s from Alpaca's (hourly check)"
            )

    async def _measure_skew(self) -> float:
        """Seconds between the system clock and Alpaca's. A reading's window includes the
        retries inside the call, so one that is out of tolerance is taken again, up to
        CLOCK_TRIES, and the one with the shortest round trip counts. A first call that fails
        raises: the clock is unverified."""
        best: tuple[timedelta, float] | None = None
        for _ in range(CLOCK_TRIES):
            before = self._clock.now()
            try:
                server = await self._d.market.clock()
            except Exception:
                if best is None:
                    raise
                break
            after = self._clock.now()
            skew = (server - (before + (after - before) / 2)).total_seconds()
            if best is None or after - before < best[0]:
                best = (after - before, skew)
            if abs(best[1]) <= MAX_CLOCK_SKEW_S:
                break
        assert best is not None  # the first call either answered or raised
        return best[1]

    async def _load_assets(self, *, final: bool) -> None:
        """The asset list R-003 checks. Filled in place, since LiveInputs holds the same dict.
        A failure at boot is tried again before the scan (DECISIONS D-051)."""
        try:
            assets = await self._d.market.assets()
        except Exception as exc:  # R-003 then rejects every entry
            log.exception("asset list unavailable")
            then = "entries are refused today" if final else "trying again before the scan"
            await self._incident("P2", "assets_unavailable", f"{type(exc).__name__}: {exc}; {then}")
            return
        self._assets.update({a.symbol: a for a in assets})

    async def _start_book(self, name: BookName) -> None:
        """Reconcile, seed on first use, raise the peak, then start the book's service."""
        d, plan = self._d, self.plan
        broker = d.brokers[name]
        records = Records(
            self._journal, book=name, calendar=d.calendar, fees=d.settings.fees, clock=self._clock
        )
        unexplained = await self._boot_reconcile(name, broker, records)
        ledger = SettlementLedger(self._journal, name, self._clock)
        if not await ledger.seeded():
            account = await broker.account()
            held = [p for p in await broker.positions() if p.qty]
            try:
                await ledger.seed(
                    account.cash, positions=len(held), open_orders=len(await broker.open_orders())
                )
            except LedgerError as exc:
                raise LedgerError(f"Book {name}: {exc}; flatten it first") from None
            log.info("ledger seeded", book=name, cash=str(account.cash))
        if FEES in d.capabilities:
            await self._book_fees(name, ledger, broker)
        equity_sod = start_of_day_cash(await ledger.entries(), self.day)
        await self._stats.raise_peak(name, equity_sod)
        service = ExecutionService(
            book=name,
            broker=broker,
            journal=self._journal,
            records=records,
            halts=self._halts,
            gate=RiskGate(d.risk, d.settings.strategy.universe),
            prices=d.hub,
            clock=self._clock,
            risk=d.risk,
            day=TradingDay(times=plan.times, next_open=plan.next_open),
            both_books=tuple(sorted(d.brokers)),
        )
        await service.start()
        assert self._inputs is not None
        store = self._jobs.store if name == "B" and self._jobs is not None else None
        if name == "B" and store is None:  # never Book B trading as Book A does
            raise RuntimeError("Book B has no verdict store")
        policy: Book
        if store is not None:  # settings.books.B.policy is llm_filtered (config enforces it)
            policy = LlmFilteredBook(
                service=service,
                inputs=self._inputs,
                journal=self._journal,
                clock=self._clock,
                strategy=d.settings.strategy,
                risk=d.risk,
                store=store,
                name=name,
            )
        else:
            policy = BaselineBook(
                service=service,
                inputs=self._inputs,
                journal=self._journal,
                clock=self._clock,
                strategy=d.settings.strategy,
                risk=d.risk,
                name=name,
            )
        self._books[name] = _Book(
            name=name,
            broker=broker,
            records=records,
            service=service,
            policy=policy,
            equity_sod=equity_sod,
            store=store,
        )
        if unexplained:
            self._spawn(self._flatten_unexplained(self._books[name]))

    async def _boot_book_b(self) -> None:
        """Book B's jobs, before its book starts: the attempts a crash left open, the LLM
        setup's fingerprint and the store rebuilt from the journal (D-068, D-069).

        A wiring error (no ``DayDeps.llm``) raises: Book B never trades without its verdicts.
        A failure while booting doesn't stop Book A's day: it is a P2, Book B is halted until
        the next open and its jobs are off, while its account is still reconciled, protected
        and flattened like any book's."""
        d, plan = self._d, self.plan
        if d.llm is None:
            raise ValueError("Book B needs its LLM runner and packs (DayDeps.llm)")
        jobs = self._jobs = BookBJobs(
            deps=d.llm,
            journal=self._journal,
            clock=self._clock,
            halts=self._halts,
            plan=plan,
            settings=d.settings,
            risk=d.risk,
            equity=lambda: self._books["B"].equity_sod if "B" in self._books else None,
        )
        try:
            await jobs.boot(
                PackContext(
                    journal=self._journal,
                    clock=self._clock,
                    market=d.market,
                    settings=d.settings,
                    risk=d.risk,
                    session=d.calendar.session(self.day),
                    previous_session=d.calendar.previous_session(self.day),
                    times=plan.times,
                    events=d.events,
                    regime=self._regime,
                    encode=d.llm.calls.encode,
                )
            )
        except Exception as exc:
            log.exception("Book B's boot failed")
            why = f"Book B's boot failed ({type(exc).__name__})"
            jobs.disable(why)
            await self._halts.halt("B", "llm_degraded", detail=why, expires_at=plan.next_open)
            await self._incident("P2", "llm_job_failed", f"{why}: no Book B entries today")

    async def _book_fees(
        self, name: BookName, ledger: SettlementLedger, broker: BrokerAdapter
    ) -> None:
        """Book what the broker charged since the last session, before E is computed (D-055).
        Fees that can't be read now are booked by EOD or the next boot."""
        try:
            rows = await ledger.book_posted_fees(broker)
        except BrokerError as exc:
            await self._incident("P3", "fees_unavailable", f"Book {name}: fees not read: {exc}")
            return
        if rows:
            net = sum((r.amount for r in rows), Decimal(0))
            log.info("fees booked", book=name, rows=len(rows), net=str(net))

    async def _boot_reconcile(self, name: BookName, broker: BrokerAdapter, records: Records) -> str:
        """Resolution 11: what today's journal explains (a restart) is adopted by the service;
        anything else (carried overnight, someone else's) is a P1, a ``p1_incident`` halt,
        and a flatten at max(now, open + 5 s). Returns what was unexplained.

        Shares the journal never heard of are journaled first as an adoption fill at the
        broker's cost (no ledger entry: the cash went before the ledger knew). Selling them
        later then leaves the journal flat, not short, so the mismatch doesn't outlive the day
        (D-048)."""
        midnight = datetime.combine(self.day, time(0), ET)
        found = await Reconciler(
            book=name, broker=broker, records=records, journal=self._journal
        ).check(since=midnight)
        today = await self._journal.read(lambda conn: read_book_day(conn, name, self.day))
        positions = [p for p in await broker.positions() if p.qty]
        held = {p.symbol: p.qty for p in positions}
        carried = {s: q for s, q in sorted(held.items()) if today.held.get(s, (0, 0))[0] != q}
        stale_orders = sorted(
            o.client_order_id
            for o in await broker.open_orders()
            if o.submitted_at is not None and o.submitted_at < midnight
        )
        problems: list[str] = []
        if carried or found.positions_differ:
            problems.append(
                f"positions {carried or found.unknown_positions or found.qty_mismatches}"
            )
        if found.unknown_orders:
            problems.append(f"orders not ours {list(found.unknown_orders)}")
        if stale_orders:
            problems.append(f"orders from earlier days {stale_orders}")
        if not problems:
            return ""
        message = f"Book {name} at boot: {'; '.join(problems)}"
        await self._incident(
            "P1", "boot_unexplained", message, {"carried": carried, "stale_orders": stale_orders}
        )
        await self._halts.halt(name, "p1_incident", detail=message)
        strangers = [p for p in positions if p.symbol in found.unknown_positions and p.qty > 0]
        if strangers:
            now = fmt_iso(self._clock.now())
            rows = [
                (f"adopt:{name}:{self.day.isoformat()}:{p.symbol}", name, p.symbol, p.qty,
                 str(p.avg_entry_price), now)
                for p in strangers
            ]  # fmt: skip
            await self._journal.write(
                lambda conn: conn.executemany(
                    "INSERT OR IGNORE INTO fills (id, broker_order_id, book, symbol, side, qty, "
                    "price, filled_at, raw_json) VALUES (?, NULL, ?, ?, 'buy', ?, ?, ?, "
                    "'{\"adopted\": true}')",
                    rows,
                )
            )
        return message

    async def _flatten_unexplained(self, b: _Book) -> None:
        """Flatten what boot couldn't explain, once the market is open (A§3: 09:30:05)."""
        times = self.plan.times
        after_open = times.open + timedelta(
            seconds=self._d.cadence.unexplained_flatten_after_open_s
        )
        await self._clock.sleep_until(max(self._clock.now(), after_open))
        while self._clock.now() < times.close:
            try:
                if (await b.service.flatten(purpose="incident")).flat:
                    return
            except Exception:
                log.exception("boot flatten failed", book=b.name)
            await self._clock.sleep_until(self._clock.now() + self._retry)

    async def _write_boot_record(self) -> None:
        """``boot.json`` (``boot-2.json`` after a restart): what a replay seeds from (D-047)."""
        recorder = self._d.recorder
        if recorder is None:
            return
        books: dict[str, Any] = {}
        for name, b in sorted(self._books.items()):
            settlement = SettlementLedger(self._journal, name, self._clock)
            ledger = await settlement.entries()
            seeded_at = await settlement.seeded_at()
            state = await self._journal.fetch_one(
                "SELECT * FROM book_state WHERE book = ?", (name,)
            )
            books[name] = {
                "ledger": [e.model_dump(mode="json") for e in ledger],
                # fee booking skips what the seed paid, so a replay needs the time (D-055)
                "seeded_at": None if seeded_at is None else fmt_iso(seeded_at),
                "halts": [h.model_dump(mode="json") for h in await self._halts.active(name)],
                "book_state": dict(zip(state.keys(), tuple(state), strict=True)) if state else None,
                "broker_cash": str((await b.broker.account()).cash),
                "equity_sod": str(b.equity_sod),
            }
        cal = self._d.calendar
        sessions = cal.sessions_between(
            max(cal.first, self.day - CALENDAR_SLICE), min(cal.last, self.day + CALENDAR_SLICE)
        )
        record = {
            "day": self.day.isoformat(),
            "started_at": fmt_iso(self._started),  # a replay starts its clock here
            "written_at": fmt_iso(self._clock.now()),
            "regime": None if self._regime is None else dataclasses.asdict(self._regime),
            "books": books,
            "calendar": [s.model_dump(mode="json") for s in sessions],
            "capabilities": sorted(self._d.capabilities),
            **self._llm_record(),
            **self._d.boot_info,
        }
        path = recorder.dir / BOOT_FILE
        n = 2
        while path.exists():
            path, n = recorder.dir / f"boot-{n}.json", n + 1
        recorder.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=1, sort_keys=True, default=str), encoding="utf-8")

    def _llm_record(self) -> dict[str, Any]:
        """``boot.json``'s LLM setup (D-069): what Book B ran with, or why it didn't run. A
        Book-A-only day without a reason has none, as before."""
        d = self._d
        if self._jobs is not None and d.llm is not None:
            fingerprint = d.llm.fingerprint
            return {
                "llm": {
                    **d.llm.setup,
                    "book_b": "on",
                    "fingerprint": None if fingerprint is None else fingerprint.sha256,
                }
            }
        if d.book_b_off:
            return {"llm": {"book_b": f"off: {d.book_b_off}"}}
        return {}

    # ---------------------------------------------------------------- PREMARKET

    async def _premarket(self) -> None:
        await self._enter("PREMARKET")
        if self._jobs is not None:  # Book B: the 09:00 scan and the 09:05 brief (never raises)
            await self._jobs.morning()
        if self._clock.now() >= self.plan.times.flatten:
            return
        await self._clock.sleep_until(self.plan.subscribe_at)
        if self._skew:  # one slow or failed answer at boot mustn't cost the whole day
            self._skew = await self._clock_skewed(final=True)
        if not self._assets:
            await self._load_assets(final=True)
        await self._subscribe()

    async def _subscribe(self) -> None:
        held: set[str] = set()
        for b in self._books.values():
            held |= set((await b.service.exposure()).symbols) | set(b.service.held)
        await self._subs.apply(held)
        if self._stale is not None:
            self._stale.started()

    # ---------------------------------------------------------------- OR_SCAN

    async def _or_scan(self) -> None:
        await self._enter("OR_SCAN")
        d, plan = self._d, self.plan
        if self._clock.now() >= plan.times.flatten:
            return
        await self._clock.sleep_until(plan.scan_at)
        result = await self._scan()
        if result is None:
            return
        self._signals = {s.id: s for s in result.signals}
        if self._skew or self._clock.now() >= plan.times.last_entry:
            return
        longs = sorted(
            (s for s in result.signals if s.direction == "long"), key=lambda s: s.rel_vol_rank
        )
        if not any(s.status in ("armed", "triggered") for s in longs):
            return
        self._engine = TriggerEngine(
            clock=self._clock,
            journal=self._journal,
            books=sorted(self._books),
            window_start=plan.times.open
            + timedelta(minutes=d.settings.strategy.selection.or_minutes),
            last_entry=plan.times.last_entry,
        )
        # Subscribe first, so the queue buffers their trades while the snapshot is taken.
        self._subs.armed = [s.symbol for s in longs if s.status == "armed"]
        self._events = d.hub.subscribe_consumer()
        await self._subscribe()
        snapshots = await self._snapshots(self._subs.armed)
        now = self._clock.now()
        already: Mapping[str, frozenset[BookName]] = {}
        if result.resumed:
            already = await decided(self._journal, self.day)
            restart = await self._engine.rearm(
                list(result.signals),
                snapshots,
                with_orders=await signals_with_orders(self._journal, self.day),
                decided=already,
            )
            missed = restart.rearmed.missed
            for book, sig in restart.lost:
                await record_skips(
                    self._journal,
                    [book],
                    [sig],
                    "restart_lost",
                    at=now,
                    detail="triggered before a restart, undecided until last entry",
                )
        else:
            missed = (await self._engine.arm(list(result.signals), snapshots)).missed
        await record_skips(self._journal, sorted(self._books), missed, "missed_at_arm", at=now)
        if self._jobs is not None:  # before Book B's worker can see a trigger (D-068)
            gone = {s.id for s in missed}
            self._jobs.start_vet(
                [
                    s
                    for s in longs
                    if s.status in ("armed", "triggered")
                    and s.id not in gone
                    and "B" not in already.get(s.id, frozenset())
                ]
            )
        self._engine_task = self._watch(
            asyncio.create_task(self._engine.run(self._events), name="triggers"),
            "the trigger engine",
            on_death=self._drop_trigger_feed,
        )

    async def _scan(self) -> ScanResult | None:
        d, plan = self._d, self.plan
        selector = OrbSipSelector(
            market=d.market,
            journal=self._journal,
            clock=self._clock,
            calendar=d.calendar,
            strategy=d.settings.strategy,
            max_chase_pct=d.risk.max_chase_pct,
            feed=d.settings.data.feed,
            ready_symbol=d.settings.data.regime_symbols[0],
            recorder=d.recorder,
        )
        late: ScanFlag | None = (
            "clock_skew"
            if self._skew
            else "late_start"
            if self._clock.now() > plan.scan_deadline
            else None
        )
        try:
            result = await selector.scan(
                self.day,
                deadline=plan.scan_deadline,
                late=late,
                features=signal_features(self._regime, self.day, d.events),
            )
        except Exception as exc:
            log.exception("scan failed")
            await self._incident(
                "P2", "scan_failed", f"{type(exc).__name__}: {exc}; no signals today"
            )
            return None
        if not result.resumed:
            if result.flag == "late_start":
                await self._incident(
                    "P2", "late_start", "started after the scan deadline: nothing armed today"
                )
            elif result.flag == "scan_late":
                await self._incident(
                    "P2",
                    "scan_deadline_missed",
                    f"scan finished after {plan.scan_deadline:%H:%M:%S}: nothing armed today",
                )
            elif result.flag is None and (result.duration_s or 0.0) > SLOW_SCAN_S:
                await self._incident("P3", "scan_slow", f"scan took {result.duration_s:.1f} s")
        return result

    async def _snapshots(self, symbols: Sequence[str]) -> dict[str, Snapshot]:
        if not symbols:
            return {}
        try:
            return await self._d.market.snapshots(list(symbols), feed=self._d.settings.data.feed)
        except Exception as exc:  # no arm check: the stream triggers them instead
            log.warning("arm snapshot failed", error=f"{type(exc).__name__}: {exc}")
            return {}

    # ---------------------------------------------------------------- TRADING

    async def _trading(self) -> None:
        await self._enter("TRADING")
        last_entry = self.plan.times.last_entry
        engine = self._engine
        if engine is not None and self._clock.now() < last_entry:
            for b in self._books.values():
                b.worker = self._watch(
                    asyncio.create_task(
                        self._work(b, engine.queues[b.name]), name=f"book-{b.name}"
                    ),
                    f"Book {b.name}'s trigger worker",
                )
            await self._clock.sleep_until(last_entry)
        if engine is not None:
            await engine.expire()
        # Anything still armed (a skewed clock, a scan after a crash) expires too.
        await self._journal.execute(
            "UPDATE signals SET status = 'expired' WHERE date = ? AND status = 'armed'",
            (self.day.isoformat(),),
        )

    async def _work(self, b: _Book, queue: "asyncio.Queue[TriggerEvent]") -> None:
        """One book's triggers, served in rank order among those waiting (D-043), one decision
        at a time. Book B may park a trigger whose verdict is pending (``Defer``, D-068): it is
        asked again, with ``waited``, once the store changes or at its deadline, in rank order
        with any new trigger. Book A never parks, so its loop is the one it always was."""
        seen = b.store.version if b.store is not None else 0
        while True:
            fresh = await self._next_triggers(b, queue, seen)
            changed = b.store is not None and b.store.version != seen
            if b.store is not None:
                seen = b.store.version  # a change during the decisions wakes the next round
            now = self._clock.now()
            again = [ev for ev, until in b.parked.values() if changed or now >= until]
            batch = [(ev, False) for ev in fresh if ev.signal_id not in b.parked]
            batch += [(ev, True) for ev in again]
            batch.sort(key=lambda pair: (pair[0].rank, pair[0].signal_id))
            b.busy = True
            try:
                for ev, waited in batch:
                    await self._decide(b, ev, waited=waited)
            finally:
                b.busy = False

    async def _next_triggers(
        self, b: _Book, queue: "asyncio.Queue[TriggerEvent]", seen: int
    ) -> list[TriggerEvent]:
        """The next triggers to serve. With triggers parked, also wake at the earliest
        deadline, or once the store's version moves past ``seen``; then possibly with none."""
        if not b.parked:
            return await next_batch(queue)
        wake = min(until for _, until in b.parked.values())
        waits: list[asyncio.Future[Any]] = [
            asyncio.ensure_future(next_batch(queue)),
            asyncio.ensure_future(self._clock.sleep_until(wake)),
        ]
        if b.store is not None:
            waits.append(asyncio.ensure_future(b.store.wait_for_change(seen)))
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()  # a queue.get() cancelled before it took an item loses none
            await asyncio.gather(*waits, return_exceptions=True)
        got = waits[0]
        return list(got.result()) if got.done() and not got.cancelled() else []

    async def _decide(self, b: _Book, ev: TriggerEvent, *, waited: bool) -> None:
        """Ask the book about one trigger; park it if it defers, forget it once decided."""
        sig = self._signals.get(ev.signal_id)
        if sig is None:
            b.parked.pop(ev.signal_id, None)
            return
        try:
            outcome = await b.policy.on_trigger(ev, sig, waited=waited)
        except Exception as exc:
            b.parked.pop(ev.signal_id, None)
            log.exception("trigger failed", book=b.name, signal=ev.signal_id)
            await self._incident(
                "P2", "trigger_failed", f"Book {b.name} {ev.signal_id}: {type(exc).__name__}"
            )
            return
        if isinstance(outcome, Defer):
            b.parked[ev.signal_id] = (ev, outcome.until)
        else:
            b.parked.pop(ev.signal_id, None)

    async def _stop_trading(self) -> None:
        """Stop the trigger engine and the book workers, never in the middle of a decision.
        One that already died was reported by ``_watch``; its error isn't raised again here,
        so the day still falls through to FLATTEN (resolution 12).

        Then every trigger Book B still has parked is asked once more. At the last entry
        each one's deadline has passed (none is later, D-068), so each gets its decision. On
        an earlier stop (the day failed) one still pending stays undecided: EOD gives it
        ``restart_lost``."""
        if self._engine_task is not None:
            self._engine_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._engine_task
            self._engine_task = None
        self._drop_trigger_feed()
        for b in self._books.values():
            if b.worker is None:
                continue
            while b.busy:
                await self._clock.sleep_until(self._clock.now() + STOP_POLL)
            b.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await b.worker
            b.worker = None
            for ev, _ in sorted(b.parked.values(), key=lambda p: (p[0].rank, p[0].signal_id)):
                await self._decide(b, ev, waited=True)
            if b.parked:
                log.warning("triggers left waiting at the stop", book=b.name,
                            signals=sorted(b.parked))  # fmt: skip
                b.parked.clear()

    def _drop_trigger_feed(self) -> None:
        """Detach the trigger engine's queue from the hub, which would otherwise keep filling
        it with every trade and quote."""
        if self._events is not None:
            self._d.hub.remove_consumer(self._events)
            self._events = None

    def _watch(
        self,
        task: "asyncio.Task[None]",
        what: str,
        *,
        on_death: Callable[[], None] | None = None,
    ) -> "asyncio.Task[None]":
        """A trading task that dies mid-day is a P2 at once, not a surprise at last entry."""

        def done(t: "asyncio.Task[None]") -> None:
            exc = None if t.cancelled() else t.exception()
            if exc is None:
                return
            log.error("trading task died", task=what, exc_info=exc)
            if on_death is not None:
                on_death()
            self._spawn(self._report_death(what, exc))

        task.add_done_callback(done)
        return task

    async def _report_death(self, what: str, exc: BaseException) -> None:
        """The P2 for a dead trading task. Spawned, so it must never raise."""
        try:
            await self._incident(
                "P2", "state_failed", f"{what} died ({type(exc).__name__}): no more entries today"
            )
        except Exception:
            log.exception("could not record the incident", task=what)

    # ---------------------------------------------------------------- FLATTEN

    async def _flatten(self) -> None:
        """Resolution 12: from the flatten time, retry until flat or the close (P1 if not flat
        by close - 2 min); after the close, one more pass leaves GTC stops on what's left."""
        await self._enter("FLATTEN")
        times = self.plan.times
        if self._clock.now() < times.close:
            await self._clock.sleep_until(times.flatten)
            await asyncio.gather(*(self._flatten_until_close(b) for _, b in self._sorted_books()))
            await self._clock.sleep_until(times.close)
            for hook in self._d.at_close:
                hook()
        for _, b in self._sorted_books():
            result = await self._after_close(b)
            if result is None:
                await self._not_flat(b, "the after-close pass failed", again=True)
            elif not result.flat:
                held = ", ".join(f"{s} {q}" for s, q in sorted(result.remaining.items()))
                stops = ", ".join(f"{s} @ {p}" for s, p in sorted(result.protected.items()))
                bare = ", ".join(f"{s} {q}" for s, q in sorted(result.unprotected.items()))
                why = f"held after the close: {held}; GTC stops: {stops or 'none'}"
                await self._not_flat(b, why + (f"; NO STOP: {bare}" if bare else ""), again=True)

    async def _after_close(self, b: _Book) -> FlattenResult | None:
        """The after-close pass, tried up to AFTER_CLOSE_TRIES times while it raises or leaves
        shares without a GTC stop: one broker error isn't a P1 on a book that may well be flat,
        nor a reason to leave shares bare overnight (DECISIONS D-051). Returns the last result;
        None when every try raised."""
        result: FlattenResult | None = None
        for attempt in range(1, AFTER_CLOSE_TRIES + 1):
            try:
                result = await b.service.flatten(purpose="eod_flatten")
            except Exception:
                log.exception("after-close flatten failed", book=b.name, attempt=attempt)
            else:
                if not result.unprotected:
                    return result
                log.warning("shares still without a GTC stop", book=b.name, bare=result.unprotected)
            if attempt < AFTER_CLOSE_TRIES:
                await self._clock.sleep_until(self._clock.now() + self._retry)
        return result

    async def _flatten_until_close(self, b: _Book) -> None:
        times = self.plan.times
        while True:
            now = self._clock.now()
            try:
                result = await b.service.flatten(
                    purpose="eod_flatten", deadline=min(now + FLATTEN_PASS, times.close)
                )
                if result.flat:
                    return
            except Exception:
                log.exception("flatten failed", book=b.name)
            if self._clock.now() >= times.flat_by:
                await self._not_flat(b, f"not flat by {times.flat_by:%H:%M}")
            if self._clock.now() >= times.close:
                return
            await self._clock.sleep_until(min(self._clock.now() + self._retry, times.close))

    async def _not_flat(self, b: _Book, why: str, *, again: bool = False) -> None:
        """RISK.md §5: a book not flat by close - 2 min is a P1, halted until a human acks.
        The after-close pass reports ``again``: what is still held and which GTC stops protect
        it is news even after the 15:58 alert."""
        first = not b.not_flat_alerted
        if not (first or again):
            return
        b.not_flat_alerted = True
        message = f"Book {b.name}: {why}"
        await self._incident("P1", "not_flat", message)
        if first:
            await self._halts.halt(b.name, "p1_incident", detail=message)

    # ---------------------------------------------------------------- EOD, REVIEW, REPORT

    async def _eod(self) -> None:
        await self._enter("EOD")
        await self._clock.sleep_until(self.plan.eod_at)
        ctx = EodContext(
            journal=self._journal,
            clock=self._clock,
            day=self.day,
            risk=self._d.risk,
            halts=self._halts,
            stats=self._stats,
            prices=self._d.hub,
            regime=signal_features(self._regime, self.day, self._d.events),
            capabilities=self._d.capabilities,
        )
        tracks = self._heartbeat.tracks if self._heartbeat is not None else {}
        # A run that started after the close sampled none of the session: unknown, not 0.
        sampled = self._started < self.plan.times.close
        until = max(self.plan.shadow_at, self._clock.now() + EOD_RETRY_WINDOW)
        for name, b in self._sorted_books():
            track = tracks.get(name)
            close = BookClose(
                book=name,
                broker=b.broker,
                records=b.records,
                max_drawdown=(track.max_drawdown if track else Decimal(0)) if sampled else None,
            )
            await self._close_book(ctx, close, until=until)
        for _, b in self._sorted_books():
            await b.service.stop()

    async def _close_book(self, ctx: EodContext, close: BookClose, *, until: datetime) -> None:
        """EOD, retried every EOD_RETRY until ``until``: its steps are marked as they finish,
        so a retry resumes where the failure stopped (DECISIONS D-051)."""
        while True:
            try:
                await close_book(ctx, close)
                return
            except Exception:
                if self._clock.now() + EOD_RETRY >= until:
                    raise
                log.exception("EOD failed; retrying", book=close.book)
            await self._clock.sleep_until(self._clock.now() + EOD_RETRY)

    async def _review(self) -> None:
        """Shadow outcomes at close + 12 min (S§7.2), then, when Book B runs, Claude's review at
        close + 15 min (S§6.1; D-069). The review runs even if the shadow job failed."""
        await self._enter("REVIEW")
        await self._clock.sleep_until(self.plan.shadow_at)
        failed: Exception | None = None
        try:
            await self._shadow()
        except Exception as exc:  # not a cancellation: a shutdown never waits for the review
            failed = exc
        if self._jobs is not None:
            await self._jobs.review()  # never raises
        if failed is not None:
            raise failed

    async def _shadow(self) -> None:
        if await step_done(self._journal, self.day, "shadow"):
            return
        strategy = self._d.settings.strategy
        run = await compute_shadow(
            journal=self._journal,
            market=self._d.market,
            clock=self._clock,
            calendar=self._d.calendar,
            day=self.day,
            times=self.plan.times,
            or_minutes=strategy.selection.or_minutes,
            params=ShadowParams(
                slippage_fixed=strategy.shadow.slippage_fixed,
                slippage_pct=strategy.shadow.slippage_pct,
            ),
            # From the journal, not the scan-time copies: they lack triggered_at (D-051).
            signals=await load_signals(self._journal, self.day),
            tape=SHADOW_TAPE in self._d.capabilities,
            # a review day: SPY's bars ride along, for the review's market move (D-071)
            spy=self._d.settings.data.regime_symbols[0]
            if self._jobs is not None and self._jobs.reviews
            else None,
        )
        if not run.ok:
            await self._incident("P3", "shadow_unavailable", run.error or "no SIP bars")
            return
        if run.tape_unresolved:
            await self._incident(
                "P3",
                "shadow_tape_unresolved",
                "counted as stopped in the trigger minute, the tape not settling it: "
                + "; ".join(run.tape_unresolved),
            )
        await mark_step(
            self._journal, self.day, "shadow", self._clock.now(), f"{len(run.outcomes)} outcomes"
        )

    async def _report(self) -> None:
        await self._enter("REPORT")
        d = self._d
        await self._clock.sleep_until(self.plan.report_at)
        await self._pump()
        if await step_done(self._journal, self.day, "report"):
            return
        report = await build_daily(
            self._journal,
            self.day,
            books=tuple(sorted(self._books)),
            mode=d.settings.mode,
            session=d.calendar.session(self.day),
            now=self._clock.now(),
            book_b_off=d.book_b_off,
            max_calls=d.settings.llm.max_calls_per_day,
            degrade_after=d.risk.llm_failures_degrade_after,
        )
        md, _ = write_daily(report, d.paths.reports_dir)
        if d.settings.notify.daily_summary:
            await d.notifier.send(f"trader {self.day}", report.summary(), tags=("bar_chart",))
        if d.recorder is not None:
            d.recorder.close()
        if self._failed:  # left unmarked: the rerun that finishes the day rebuilds it (D-051)
            log.warning("report written without the failed states", failed=self._failed)
            return
        await mark_step(self._journal, self.day, "report", self._clock.now(), str(md.name))

    async def _backup(self) -> None:
        """SQLite's online backup, keeping ``db_backups_keep`` copies (A§3 16:30)."""
        backups = self._d.paths.backups_dir
        try:
            await self._journal.backup(backups / f"trader-{self.day.isoformat()}.db")
            keep = self._d.settings.storage.db_backups_keep
            for old in sorted(backups.glob("trader-*.db"))[:-keep]:
                old.unlink()
        except (OSError, sqlite3.Error) as exc:  # best effort, before or after DONE
            log.warning("database backup failed", error=f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- helpers

    @property
    def _retry(self) -> timedelta:
        return timedelta(seconds=self._d.cadence.flatten_retry_s)

    def _sorted_books(self) -> list[tuple[BookName, _Book]]:
        return sorted(self._books.items())

    def _spawn(self, job: Coroutine[Any, Any, None]) -> None:
        self._tasks.append(asyncio.create_task(job))

    async def _kill_flatten(self) -> bool:
        """The kill switch: flatten every book; True once all are flat."""
        flat = True
        for _, b in self._sorted_books():
            try:
                flat = (await b.service.flatten(purpose="kill_switch")).flat and flat
            except Exception:
                log.exception("kill-switch flatten failed", book=b.name)
                flat = False
        return flat

    async def _pump(self) -> None:
        sent, failed = await pump_alerts(self._journal, self._d.notifier, self._clock)
        if sent or failed:
            log.info("alerts pumped", sent=sent, failed=failed)

    async def _incident(
        self,
        severity: Severity,
        code: str,
        message: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        await record_incident(
            self._journal,
            at=self._clock.now(),
            severity=severity,
            component="orchestrator",
            code=code,
            message=message,
            detail=detail,
        )

    async def _shutdown(self) -> None:
        if self._jobs is not None:
            try:
                await self._jobs.close()  # a vet still out is abandoned; the news stops
            except Exception:
                log.exception("Book B's jobs did not close cleanly")
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # a finished day's outcome mustn't turn into a traceback here
                log.exception("background task failed")
        self._tasks = []
        for _, b in self._sorted_books():
            await b.service.stop()
        await self._d.hub.stop()
        if self._d.recorder is not None:
            self._d.recorder.close()

    # ---------------------------------------------------------------- --dry-run

    async def dry_run(self) -> list[str]:
        """Boot's checks, read-only, plus the day's timeline (``run-day --dry-run``)."""
        d = self._d
        if not d.calendar.is_trading_day(self.day):
            return [f"{self.day} is not a trading day: run-day would exit"]
        plan = day_plan(d.calendar, self.day, d.settings, d.risk)
        lines = plan.lines(book_b="B" in d.brokers)
        skew = await self._measure_skew()
        verdict = "ok" if abs(skew) <= MAX_CLOCK_SKEW_S else "TOO FAR: nothing would be armed"
        lines.append(f"clock skew {skew:+.2f} s ({verdict})")
        row = await self._journal.fetch_one(
            "SELECT COUNT(*) AS n FROM universe_daily WHERE date = ? AND eligible = 1",
            (self.day.isoformat(),),
        )
        eligible = row["n"] if row is not None else 0
        lines.append(
            f"universe: {eligible} eligible"
            if eligible
            else "universe: not built yet (run-day builds it at boot)"
        )
        tags = await load_regime(self._journal, d.calendar.previous_session(self.day))
        lines.append(
            f"regime t-1: {tags.spy_trend}/{tags.vol_bucket}"
            if tags
            else "regime t-1: not computed yet (run-day computes it at boot)"
        )
        for name in sorted(d.brokers):
            broker = d.brokers[name]
            account = await broker.account()
            held = {p.symbol: p.qty for p in await broker.positions() if p.qty}
            orders = await broker.open_orders()
            ledger = SettlementLedger(self._journal, name, self._clock)
            seeded = await ledger.seeded()
            equity = start_of_day_cash(await ledger.entries(), self.day) if seeded else None
            halts = [h.reason for h in await self._halts.active(name)]
            lines.append(
                f"Book {name}: cash {account.cash}, equity {account.equity}, "
                f"{len(held)} positions{f' {held}' if held else ''}, {len(orders)} open orders; "
                f"ledger {'E ' + str(equity) if seeded else 'not seeded (seeds at boot)'}; "
                f"halts {', '.join(halts) or 'none'}"
            )
        if d.paths.kill_file.exists():
            lines.append(f"KILL FILE PRESENT ({d.paths.kill_file}): every book would halt")
        return lines
