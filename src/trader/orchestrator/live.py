"""Wiring a real trading day: ``trader run-day`` (A§1, A§4.11; DECISIONS D-048).

- **Paper (default):** Alpaca REST and one Alpaca paper account per book. Every REST call,
  every broker call and trade update, and every stream event is recorded for replay (D-047).
  ``runtime/run-day.lock`` keeps a second engine off the same accounts.
- **``--sim``:** live market data and the real clock, with a SimBroker per book fed by the hub.
  Each simulated day has its own folder, ``runtime/sim/<date>/`` (database, logs, recordings,
  reports, heartbeat, lock), seeded with today's universe from the main database when it has
  one. It watches the real kill file, and its alerts start with ``[SIM]``.
- **``--dry-run``:** boot's checks, read-only, and the day's timeline. Nothing is traded or
  recorded, and no lock is taken.

Book B is refused until Phase 4.
"""

import asyncio
import hashlib
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack, closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from trader import __version__
from trader.config import RISK_FILE, SETTINGS_FILE, AppConfig, BookName, load_config
from trader.core.calendar import (
    CalendarRangeError,
    NotATradingDayError,
    TradingCalendar,
    default_sync_range,
    sync_calendar,
)
from trader.core.clock import ET, Clock, RealClock
from trader.core.events import EVENTS_FILE, load_events
from trader.core.logging import configure_logging, shutdown_logging
from trader.core.models import Quote, Trade
from trader.data import ws
from trader.data.alpaca_rest import AlpacaRest
from trader.data.recorder import REST_FILE, Recorder, broker_file
from trader.data.replay import RecordingMarketData
from trader.data.stream import TRADING_CHANNELS, MarketDataHub, MarketEvent
from trader.data.universe import UniverseBuilder
from trader.data.ws import StreamError
from trader.execution.alpaca_broker import AlpacaBroker
from trader.execution.broker import BrokerAdapter
from trader.execution.recording import RecordingBroker
from trader.execution.sim_broker import SimBroker
from trader.memory.incidents import record_incident
from trader.memory.journal import Journal
from trader.notify import notifier_from_config
from trader.orchestrator.day import DayDeps, DayOrchestrator, DayPaths
from trader.orchestrator.lock import RunLock
from trader.orchestrator.ports import DayMarket
from trader.orchestrator.provenance import source_sha256
from trader.risk.killswitch import KILL_FILE

log = structlog.get_logger(__name__)

SIM_CASH = Decimal(1000)  # the experiment's budget (CLAUDE.md)
Echo = Callable[[str], None]


class BookChoiceError(ValueError):
    """``--books`` asked for something run-day can't do yet."""


def parse_books(choice: str) -> tuple[BookName, ...]:
    picked = {part.strip().upper() for part in choice.split(",") if part.strip()}
    if picked - {"A", "B"} or not picked:
        raise BookChoiceError(f"--books takes A (and B from Phase 4), not {choice!r}")
    if "B" in picked:
        raise BookChoiceError("Book B arrives in Phase 4 (STRATEGY.md section 5): use --books A")
    return ("A",)


@dataclass(frozen=True)
class RunOptions:
    books: tuple[BookName, ...]
    sim: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class Folders:
    db: Path
    logs: Path
    recordings: Path
    reports: Path
    backups: Path
    heartbeat: Path
    lock: Path


def folders(cfg: AppConfig, day: date, *, sim: bool) -> Folders:
    storage = cfg.settings.storage
    if not sim:
        db = cfg.path(storage.db_path)
        return Folders(
            db=db,
            logs=cfg.path(storage.logs_dir),
            recordings=cfg.path(storage.recordings_dir),
            reports=cfg.path(storage.reports_dir),
            backups=db.parent / "backups",
            heartbeat=db.parent / "heartbeat.json",
            lock=db.parent / "run-day.lock",
        )
    base = cfg.path(storage.db_path).parent / "sim" / day.isoformat()
    return Folders(
        db=base / "trader.db",
        logs=base / "logs",
        recordings=base / "recordings",
        reports=base / "reports",
        backups=base / "backups",
        heartbeat=base / "heartbeat.json",
        lock=base / "run-day.lock",
    )


async def run_day(root: Path, opts: RunOptions, echo: Echo, *, clock: Clock | None = None) -> None:
    """Run (or check) today's session. Raises on a failure the caller reports."""
    cfg = load_config(root)
    if cfg.settings.mode != "paper":
        raise BookChoiceError("live trading needs the Phase 7 gate (RISK.md section 7)")
    clock = clock or RealClock()
    today = clock.now().date()
    where = folders(cfg, today, sim=opts.sim)
    configure_logging(clock=clock, secrets=cfg.secrets.redaction_map(), logs_dir=where.logs)
    async with AsyncExitStack() as stack:
        stack.callback(shutdown_logging)
        if not opts.dry_run:
            lock = RunLock(where.lock)
            lock.acquire()
            stack.callback(lock.release)
        journal = await stack.enter_async_context(await Journal.open(where.db))
        rest = await stack.enter_async_context(AlpacaRest.from_config(cfg, clock))
        first, last = default_sync_range(today)
        calendar, _ = await sync_calendar(rest.calendar, journal, first, last)
        if not calendar.is_trading_day(today):
            echo(f"{today} is not a trading day: nothing to do")
            return
        if opts.sim:
            await _copy_universe(cfg.path(cfg.settings.storage.db_path), where.db, today, journal)
        recorder = None if opts.dry_run else Recorder(where.recordings, today)
        if recorder is not None:
            stack.callback(recorder.close)
        market: DayMarket = rest
        if recorder is not None:
            market = RecordingMarketData(
                rest, clock, lambda record: recorder.append(REST_FILE, record)
            )
        brokers = await _brokers(cfg, opts, clock, calendar, recorder, stack)
        incidents = _IncidentSink(journal, clock)
        hub = _hub(cfg, clock, calendar, incidents.stream_error)
        if recorder is not None:
            hub.add_tap(lambda event: recorder.record_event(event, rx=clock.now()))
        for broker in brokers.values():
            if isinstance(broker, SimBroker):
                hub.add_tap(_feeder(broker))
        midnight = datetime.combine(today, time(0), ET)
        events = load_events(root / EVENTS_FILE).between(midnight, midnight + timedelta(days=1))
        deps = DayDeps(
            settings=cfg.settings,
            risk=cfg.risk,
            clock=clock,
            journal=journal,
            calendar=calendar,
            market=market,
            hub=hub,
            brokers=brokers,
            notifier=notifier_from_config(cfg, prefix="[SIM] " if opts.sim else ""),
            events=tuple(events),
            paths=DayPaths(
                kill_file=root / KILL_FILE,
                heartbeat=where.heartbeat,
                reports_dir=where.reports,
                backups_dir=where.backups,
            ),
            recorder=recorder,
            ensure_universe=_universe_builder(cfg, rest, journal, calendar, recorder),
            at_close=tuple(
                b.expire_day_orders for b in brokers.values() if isinstance(b, SimBroker)
            ),
            boot_info=_boot_info(root, opts),
        )
        orchestrator = DayOrchestrator(deps)
        if opts.dry_run:
            for line in await orchestrator.dry_run():
                echo(line)
            return
        echo(
            f"run-day {today}: Book {', '.join(opts.books)}{' (sim)' if opts.sim else ''}; "
            f"logs in {where.logs}"
        )
        outcome = await orchestrator.run()
        echo(
            f"run-day {outcome.day}: {outcome.state}{' - ' + outcome.note if outcome.note else ''}"
        )


async def _brokers(
    cfg: AppConfig,
    opts: RunOptions,
    clock: Clock,
    calendar: TradingCalendar,
    recorder: Recorder | None,
    stack: AsyncExitStack,
) -> dict[BookName, BrokerAdapter]:
    out: dict[BookName, BrokerAdapter] = {}
    for book in opts.books:
        if opts.sim:
            out[book] = SimBroker(
                clock=clock, book=book, cash=SIM_CASH, market_open=_market_open(calendar)
            )
            continue
        broker: BrokerAdapter = AlpacaBroker.from_config(cfg, book, clock)
        stack.push_async_callback(broker.aclose)
        if recorder is not None:
            broker = RecordingBroker(broker, clock, _sink(recorder, broker_file(book)))
        out[book] = broker
    return out


def _sink(recorder: Recorder, name: str) -> Callable[[dict[str, Any]], None]:
    def append(record: dict[str, Any]) -> None:
        recorder.append(name, record)

    return append


def _hub(
    cfg: AppConfig,
    clock: Clock,
    calendar: TradingCalendar,
    on_error: Callable[[StreamError], None],
) -> MarketDataHub:
    key, secret = cfg.data_credentials()
    if key is None or secret is None:  # AlpacaRest.from_config already refused this
        raise BookChoiceError("no market-data keys in .env")
    return MarketDataHub(
        key_id=key.get_secret_value(),
        secret_key=secret.get_secret_value(),
        feed=cfg.settings.data.feed,
        clock=clock,
        symbol_limit=cfg.settings.data.stream_symbol_limit,
        market_open=_market_open(calendar),
        connector=ws.websockets_connector,
        channels=TRADING_CHANNELS,
        on_stream_error=on_error,
    )


def _market_open(calendar: TradingCalendar) -> Callable[[datetime], bool]:
    """Regular hours from the calendar, early closes included (resolution 13)."""

    def is_open(now: datetime) -> bool:
        try:
            session = calendar.session(now.date())
        except (CalendarRangeError, NotATradingDayError):
            return False
        return session.open <= now < session.close

    return is_open


def _feeder(sim: SimBroker) -> Callable[[MarketEvent], None]:
    def feed(event: MarketEvent) -> None:
        if isinstance(event, (Trade, Quote)):
            sim.feed(event)

    return feed


class _IncidentSink:
    """Journals incidents raised from synchronous callbacks (the stream's error hook)."""

    def __init__(self, journal: Journal, clock: Clock) -> None:
        self._journal, self._clock = journal, clock
        self._pending: set[asyncio.Task[int]] = set()

    def stream_error(self, error: StreamError) -> None:
        """Resolution 13: a server error on the replayed subscriptions is a P2."""
        task = asyncio.get_running_loop().create_task(
            record_incident(
                self._journal,
                at=self._clock.now(),
                severity="P2",
                component="data",
                code="subscription_refused",
                message=f"stream error {error.code}: {error.message}",
            )
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)


def _universe_builder(
    cfg: AppConfig,
    rest: AlpacaRest,
    journal: Journal,
    calendar: TradingCalendar,
    recorder: Recorder | None,
) -> Callable[[date], Any]:
    """Build today's universe if nobody did (A§3 07:30), over the unrecorded client: replay
    takes the universe from ``universe.parquet`` instead of thousands of recorded bars."""

    async def ensure(day: date) -> int:
        row = await journal.fetch_one(
            "SELECT COUNT(*) AS n FROM universe_daily WHERE date = ? AND eligible = 1",
            (day.isoformat(),),
        )
        if row is not None and row["n"]:
            return int(row["n"])
        builder = UniverseBuilder(
            data=rest,
            journal=journal,
            calendar=calendar,
            universe=cfg.settings.strategy.universe,
            feed=cfg.settings.data.feed,
        )
        report, rows = await builder.build(day, persist=True)
        if recorder is not None:
            recorder.write_universe(rows)
        return report.eligible

    return ensure


async def _copy_universe(main_db: Path, sim_db: Path, day: date, journal: Journal) -> None:
    """A simulated day starts from today's universe in the main database, if it was built."""
    rows = _universe_rows(main_db, sim_db, day)
    if rows:
        await journal.write(
            lambda conn: conn.executemany(
                "INSERT OR IGNORE INTO universe_daily (date, symbol, name, exchange, prev_close, "
                "atr14, adv14, or_vol_avg14, eligible, exclusion_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        )


def _universe_rows(main_db: Path, sim_db: Path, day: date) -> list[tuple[Any, ...]]:
    if not main_db.exists() or main_db == sim_db:
        return []
    with closing(sqlite3.connect(f"file:{main_db.as_posix()}?mode=ro", uri=True)) as source:
        rows: list[tuple[Any, ...]] = source.execute(
            "SELECT date, symbol, name, exchange, prev_close, atr14, adv14, or_vol_avg14, "
            "eligible, exclusion_reason FROM universe_daily WHERE date = ?",
            (day.isoformat(),),
        ).fetchall()
    return rows


def _boot_info(root: Path, opts: RunOptions) -> Mapping[str, Any]:
    """What ``boot.json`` says about the code and config the day ran with (never secrets)."""
    files = {"settings": SETTINGS_FILE, "risk": RISK_FILE, "events": EVENTS_FILE}
    hashes = {
        f"{name}_sha256": hashlib.sha256((root / rel).read_bytes()).hexdigest()
        for name, rel in files.items()
        if (root / rel).is_file()
    }
    return {
        "version": __version__,
        "git_sha": _git_sha(root),
        "source_sha256": source_sha256(root),  # names an uncommitted tree too (D-055)
        "sim": opts.sim,
        **hashes,
    }


def _git_sha(root: Path) -> str | None:
    """HEAD's commit, read from .git without running git."""
    head = root / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if not ref.startswith("ref: "):
            return ref
        target = root / ".git" / ref.removeprefix("ref: ")
        if target.is_file():
            return target.read_text(encoding="utf-8").strip()
        packed = root / ".git" / "packed-refs"
        for line in packed.read_text(encoding="utf-8").splitlines() if packed.is_file() else ():
            sha, _, name = line.partition(" ")
            if name == ref.removeprefix("ref: "):
                return sha
    except OSError:
        return None
    return None
