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

**Book B** (``--books A,B``; D-069) runs with Claude through ``LlmCalls``:
- **Paper:** the real CLI (``ClaudeCliTransport``), its answers recorded (``RecordingTransport``)
  with the packs (``RecordingPacks``) into ``llm.jsonl.gz``; ``NewsHub`` from boot, its articles
  recorded; EDGAR for the filings.
- **``--sim``:** a scripted model that finds every candidate neutral, with an environment holding
  no credential, and its own sandbox and artifacts under the simulated day's folder.
- **Off** for the day with ``--no-llm``, ``llm.enabled: false`` or ``books.B.enabled: false``:
  Book A runs alone, and the boot record and the report say why. So it does, with a P2, when the
  CLI or its credential is missing.
"""

import asyncio
import hashlib
import os
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
from trader.data.edgar import EdgarClient, EdgarError
from trader.data.news import NewsHub
from trader.data.recorder import REST_FILE, Recorder, broker_file
from trader.data.replay import RecordingMarketData
from trader.data.stream import TRADING_CHANNELS, MarketDataHub, MarketEvent
from trader.data.universe import UniverseBuilder
from trader.data.ws import StreamError
from trader.execution.alpaca_broker import AlpacaBroker
from trader.execution.broker import BrokerAdapter
from trader.execution.recording import RecordingBroker
from trader.execution.sim_broker import SimBroker
from trader.llm.claude_cli import (
    ClaudeBinaryError,
    ClaudeCliTransport,
    Transport,
    build_claude_env,
    parse_version,
    resolve_claude,
)
from trader.llm.client import LlmCalls, LlmUnavailableError, credentialed_env
from trader.llm.epoch import epoch_fingerprint
from trader.llm.fake import ScriptedTransport, valid
from trader.llm.replay import LLM_FILE, RecordingTransport
from trader.llm.smoke import cli_version
from trader.memory.incidents import record_incident
from trader.memory.journal import Journal
from trader.memory.reviews import DailyReviewWork
from trader.notify import notifier_from_config
from trader.orchestrator.day import DayDeps, DayOrchestrator, DayPaths
from trader.orchestrator.evidence import EvidenceDesk
from trader.orchestrator.llm_jobs import LlmDeps
from trader.orchestrator.lock import LOCK_NAME, RunLock
from trader.orchestrator.packs import PackFactory, live_packs, recording_packs
from trader.orchestrator.ports import DayMarket, FilingsPort
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
        raise BookChoiceError(f"--books takes A, B or A,B, not {choice!r}")
    return tuple(book for book in ("A", "B") if book in picked)


@dataclass(frozen=True)
class RunOptions:
    books: tuple[BookName, ...]
    sim: bool = False
    dry_run: bool = False
    no_llm: bool = False  # Book B off for the day (plan decision 9)


def book_b_off(cfg: AppConfig, opts: RunOptions) -> str | None:
    """Why Book B won't run today, though asked for; None when it runs or wasn't asked."""
    if "B" not in opts.books:
        return None
    if opts.no_llm:
        return "--no-llm"
    if not cfg.settings.llm.enabled:
        return "llm.enabled is false"
    if not cfg.settings.books.B.enabled:
        return "books.B.enabled is false"
    return None


@dataclass(frozen=True)
class Folders:
    db: Path
    logs: Path
    recordings: Path
    reports: Path
    backups: Path
    heartbeat: Path
    lock: Path
    llm_sandbox: Path
    llm_artifacts: Path


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
            lock=db.parent / LOCK_NAME,
            llm_sandbox=cfg.path(cfg.settings.llm.sandbox_dir),
            llm_artifacts=cfg.path(storage.llm_artifacts_dir),
        )
    base = cfg.path(storage.db_path).parent / "sim" / day.isoformat()
    return Folders(
        db=base / "trader.db",
        logs=base / "logs",
        recordings=base / "recordings",
        reports=base / "reports",
        backups=base / "backups",
        heartbeat=base / "heartbeat.json",
        lock=base / LOCK_NAME,
        llm_sandbox=base / "llm_sandbox",  # ids repeat: never the live day's artifacts (D-064)
        llm_artifacts=base / "llm",
    )


async def run_day(root: Path, opts: RunOptions, echo: Echo, *, clock: Clock | None = None) -> None:
    """Run (or check) today's session. Raises on a failure the caller reports."""
    cfg = load_config(root)
    if cfg.settings.mode != "paper":
        raise BookChoiceError("live trading needs the Phase 7 gate (RISK.md section 7)")
    off = book_b_off(cfg, opts)
    books = tuple(b for b in opts.books if b != "B" or off is None)
    if not books:
        raise BookChoiceError(f"Book B is off ({off}) and no other book was asked for")
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
        llm: LlmDeps | None = None
        if "B" in books and opts.dry_run:
            echo(_llm_ready(cfg, opts, books))
        elif "B" in books:
            try:
                llm = await _book_b_llm(cfg, opts, clock, journal, rest, recorder, where, stack)
            except LlmUnavailableError as exc:  # Book A still trades; Book B can't
                off, books = f"the LLM is unavailable: {exc}", tuple(b for b in books if b != "B")
                await record_incident(
                    journal,
                    at=clock.now(),
                    severity="P2",
                    component="llm",
                    code="llm_unavailable",
                    message=f"Book B is off today: {exc}",
                )
                if not books:
                    raise BookChoiceError(
                        f"Book B is off ({off}) and no other book was asked for"
                    ) from exc
        if off is not None:
            echo(f"Book B is off today: {off}")
            if not (opts.sim or opts.dry_run):
                await _check_idle_book(cfg, "B", clock, journal, echo)
        brokers = await _brokers(cfg, books, opts, clock, calendar, recorder, stack)
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
            llm=llm,
            book_b_off=off,
        )
        orchestrator = DayOrchestrator(deps)
        if opts.dry_run:
            for line in await orchestrator.dry_run():
                echo(line)
            return
        echo(
            f"run-day {today}: Book {', '.join(books)}{' (sim)' if opts.sim else ''}; "
            f"logs in {where.logs}"
        )
        outcome = await orchestrator.run()
        echo(
            f"run-day {outcome.day}: {outcome.state}{' - ' + outcome.note if outcome.note else ''}"
        )


async def _brokers(
    cfg: AppConfig,
    books: tuple[BookName, ...],
    opts: RunOptions,
    clock: Clock,
    calendar: TradingCalendar,
    recorder: Recorder | None,
    stack: AsyncExitStack,
) -> dict[BookName, BrokerAdapter]:
    out: dict[BookName, BrokerAdapter] = {}
    for book in books:
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


# ---------------------------------------------------------------- Book B (D-069)


async def _book_b_llm(
    cfg: AppConfig,
    opts: RunOptions,
    clock: Clock,
    journal: Journal,
    rest: AlpacaRest,
    recorder: Recorder | None,
    where: Folders,
    stack: AsyncExitStack,
) -> LlmDeps:
    """Book B's runner, news, EDGAR and packs. Raises ``LlmUnavailableError`` when the CLI or
    its credential is missing (paper; ``--sim`` needs neither)."""
    llm = cfg.settings.llm
    transport: Transport
    if opts.sim:
        transport = ScriptedTransport(valid, sleep=_clock_sleep(clock))
        command: tuple[str, ...] = ("sim",)
        env = build_claude_env(parent=os.environ, config_dir=cfg.path(llm.config_dir),
                               oauth_token=None)  # fmt: skip
        version: str | None = None
    else:
        try:
            command = (str(resolve_claude(llm.claude_bin)),)
        except ClaudeBinaryError as exc:
            raise LlmUnavailableError(str(exc)) from exc
        env = credentialed_env(cfg, os.environ)
        where.llm_sandbox.mkdir(parents=True, exist_ok=True)
        # not a model call (CLAUDE.md invariant 5): the version every llm_calls row records
        found = parse_version(await asyncio.to_thread(cli_version, command, env, where.llm_sandbox))
        version = ".".join(map(str, found)) if found else None
        tested = parse_version(llm.max_tested_cli_version or "")
        if found is not None and tested is not None and found > tested:
            await record_incident(
                journal,
                at=clock.now(),
                severity="P3",
                component="llm",
                code="llm_cli_untested",
                message=f"claude {version} is newer than llm.max_tested_cli_version "
                f"{llm.max_tested_cli_version}: rerun `trader smoke llm` (G4a's runbook)",
            )
        transport = ClaudeCliTransport(kill_grace_s=llm.kill_grace_s)
    sink: Callable[[dict[str, Any]], None] | None = None
    if recorder is not None:
        llm_recorder = recorder

        def sink(record: dict[str, Any]) -> None:
            llm_recorder.append(LLM_FILE, record, flush=True)

        transport = RecordingTransport(transport, clock, sink)
    calls = LlmCalls.from_config(
        cfg,
        journal=journal,
        clock=clock,
        transport=transport,
        command=command,
        env=env,
        sandbox_dir=where.llm_sandbox,
        artifacts_dir=where.llm_artifacts,
        cli_version=version,
    )
    key, secret = cfg.data_credentials()
    if key is None or secret is None:  # AlpacaRest.from_config already refused this
        raise BookChoiceError("no market-data keys in .env")
    news = NewsHub(
        rest=rest,
        journal=journal,
        clock=clock,
        key_id=key.get_secret_value(),
        secret_key=secret.get_secret_value(),
        on_item=None if recorder is None else recorder.record_news,
        sleep=_clock_sleep(clock),
    )
    filings, edgar = _edgar(cfg, clock, journal, stack)
    if filings is None:
        await record_incident(
            journal,
            at=clock.now(),
            severity="P3",
            component="llm",
            code="edgar_unavailable",
            message=f"EDGAR is {edgar}: Book B's packs show every symbol's filings as unknown",
        )
    desk = EvidenceDesk(
        news=news,
        filings=filings,
        clock=clock,
        filings_days=cfg.settings.edgar.lookback_days,
        news_up=lambda: news.mode in ("stream", "poll"),
    )
    review = DailyReviewWork(journal=journal, clock=clock, settings=cfg.settings,
                             day=clock.now().date())  # fmt: skip
    packs: PackFactory = live_packs(desk, review=review)
    if sink is not None:
        packs = recording_packs(packs, sink)
    roles = llm.roles
    setup = {
        "transport": "scripted" if opts.sim else "claude_cli",
        "provider": llm.provider,
        "cli_version": version,
        "schema_mode": llm.schema_mode,
        "tools_flag": llm.tools_flag,
        "safe_mode": llm.safe_mode,
        "models": {
            "premarket": f"{roles.premarket.model}/{roles.premarket.effort}",
            "vet": f"{roles.vet.model}/{roles.vet.effort}",
            "review": f"{roles.review.model}/{roles.review.effort}",
        },
        "edgar": edgar,
    }
    return LlmDeps(
        calls=calls,
        packs=packs,
        news=news,
        review=review,
        fingerprint=epoch_fingerprint(cfg.settings, cfg.root / "prompts"),
        setup=setup,
    )


def _edgar(
    cfg: AppConfig, clock: Clock, journal: Journal, stack: AsyncExitStack
) -> tuple[FilingsPort | None, str]:
    """The EDGAR client, or None (filings unknown in every pack) without a usable user agent."""
    try:
        client = EdgarClient.from_config(cfg, clock, journal)
    except EdgarError as exc:
        log.warning("EDGAR unavailable: Book B's packs show filings as unknown", error=str(exc))
        return None, f"off: {exc}"
    stack.push_async_callback(client.aclose)
    return client, "on"


def _llm_ready(cfg: AppConfig, opts: RunOptions, books: tuple[BookName, ...]) -> str:
    """``--dry-run``'s line on Book B's LLM: found, or what run-day would do without it."""
    if opts.sim:
        return "Book B's LLM: scripted (--sim): every candidate neutral, no claude call"
    llm = cfg.settings.llm
    try:
        exe = resolve_claude(llm.claude_bin)
        credentialed_env(cfg, os.environ)
    except (ClaudeBinaryError, LlmUnavailableError) as exc:
        then = "run Book A alone" if "A" in books else "refuse to run: no other book was asked for"
        return f"Book B's LLM: UNAVAILABLE ({exc}): run-day would {then}"
    return f"Book B's LLM: {llm.provider}, {exe}; credential set"


async def _check_idle_book(
    cfg: AppConfig, book: BookName, clock: Clock, journal: Journal, echo: Echo
) -> None:
    """A book kept off today is reconciled, protected and flattened by nobody: its account
    must be flat. A read-only look at it; anything held or working is a P2 (D-069)."""
    try:
        broker = AlpacaBroker.from_config(cfg, book, clock)
    except Exception as exc:
        echo(f"Book {book}'s account was not checked: {type(exc).__name__}")
        return
    try:
        held = {p.symbol: p.qty for p in await broker.positions() if p.qty}
        orders = await broker.open_orders()
    except Exception as exc:
        echo(f"Book {book}'s account was not checked: {type(exc).__name__}")
        return
    finally:
        close = getattr(broker, "aclose", None)
        if close is not None:
            await close()
    if not (held or orders):
        return
    message = (
        f"Book {book} is off today, but its account holds {held or 'no positions'} and "
        f"{len(orders)} open orders, which nothing watches: flatten it "
        f"(`trader flatten --book {book}`) or run it"
    )
    echo(message)
    await record_incident(journal, at=clock.now(), severity="P2", component="orchestrator",
                          code="idle_book_not_flat", message=message)  # fmt: skip


def _clock_sleep(clock: Clock) -> Callable[[float], Any]:
    """A sleep on the injected clock, for parts that take one in seconds."""

    async def sleep(seconds: float) -> None:
        await clock.sleep_until(clock.now() + timedelta(seconds=seconds))

    return sleep


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
