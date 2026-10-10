"""`trader` command-line interface."""

import asyncio
import datetime as dt
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any, Literal

import typer

from trader import __version__

app = typer.Typer(
    name="trader",
    help="ai-day-trader: long-only ORB experiment. Paper by default.",
    no_args_is_help=True,
    add_completion=False,
    # Rich tracebacks print local variables by default, and locals can hold secrets.
    pretty_exceptions_show_locals=False,
)
universe_app = typer.Typer(
    help="Daily universe and opening-range baselines (STRATEGY.md 3.1).", no_args_is_help=True
)
app.add_typer(universe_app, name="universe")
smoke_app = typer.Typer(
    help="End-to-end checks against real accounts. HUMAN GATES only.", no_args_is_help=True
)
app.add_typer(smoke_app, name="smoke")

DateOption = Annotated[
    str | None,
    typer.Option("--date", help="Session date YYYY-MM-DD. Default: the next session to open."),
]


def _print_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    root: Annotated[
        Path,
        typer.Option(
            envvar="TRADER_ROOT",
            help="Repo root holding config/, runtime/ and .env. Defaults to the current directory.",
        ),
    ] = Path("."),
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_print_version, is_eager=True, help="Print the version and exit."
        ),
    ] = False,
) -> None:
    """ai-day-trader command line."""
    ctx.obj = root.resolve()


@app.command()
def doctor(
    ctx: typer.Context,
    quick: Annotated[
        bool, typer.Option("--quick", help="Skip checks that start subprocesses (uv, claude).")
    ] = False,
) -> None:
    """Check config, .env key names, both Alpaca paper accounts, clock skew, the claude CLI,
    runtime dirs, token age, and events.yaml coverage. Exits 1 on any FAIL."""
    from trader.doctor import default_deps, format_report, run_doctor  # alpaca-py loads slowly

    checks = run_doctor(ctx.obj, default_deps(), quick=quick)
    for line in format_report(checks):
        typer.echo(line)
    if any(check.status == "FAIL" for check in checks):
        raise typer.Exit(code=1)


@universe_app.command("build")
def universe_build(ctx: typer.Context, date: DateOption = None) -> None:
    """Compute eligibility for a session from prior sessions and store it in universe_daily."""
    _run(_universe(ctx.obj, date, persist=True))


@universe_app.command("or-baseline")
def universe_or_baseline(ctx: typer.Context, date: DateOption = None) -> None:
    """Backfill the 14-session opening-range baselines without writing the universe."""
    _run(_universe(ctx.obj, date, persist=False))


async def _universe(root: Path, date: str | None, *, persist: bool) -> None:
    from trader.data.recorder import Recorder
    from trader.data.universe import UniverseBuilder, default_target
    from trader.runtime import open_runtime

    async with open_runtime(root) as rt:
        calendar = await rt.calendar()
        target = dt.date.fromisoformat(date) if date else default_target(calendar, rt.clock.now())
        builder = UniverseBuilder(
            data=rt.rest,
            journal=rt.journal,
            calendar=calendar,
            universe=rt.cfg.settings.strategy.universe,
            feed=rt.cfg.settings.data.feed,
        )
        report, rows = await builder.build(target, persist=persist)
        if persist:
            recordings = rt.cfg.path(rt.cfg.settings.storage.recordings_dir)
            Recorder(recordings, target).write_universe(rows)
            for line in report.lines():
                typer.echo(line)
        else:
            with_baseline = sum(row.or_vol_avg14 is not None for row in rows)
            typer.echo(
                f"OR baseline before {target}: {with_baseline} symbols have 14 sessions "
                f"({rt.cfg.settings.data.feed} feed); universe not written"
            )


@app.command()
def stream(
    ctx: typer.Context,
    symbols: Annotated[
        str | None,
        typer.Option(
            "--symbols", help="Comma-separated. Default SPY,QQQ (FAKEPACA on --feed test)."
        ),
    ] = None,
    minutes: Annotated[float, typer.Option("--minutes", min=0.0, help="How long to listen.")] = 2.0,
    feed: Annotated[
        str | None,
        typer.Option("--feed", help="iex, sip, or test (always-on fake data). Default: settings."),
    ] = None,
    news: Annotated[
        bool,
        typer.Option("--news", help="Also follow the news stream (shows if both may connect)."),
    ] = False,
) -> None:
    """Dev: print live minute bars with per-minute trade counts, then a summary."""
    _run(_stream(ctx.obj, symbols, minutes, feed, with_news=news))


@app.command()
def news(
    ctx: typer.Context,
    symbols: Annotated[str, typer.Option("--symbols", help="Comma-separated, e.g. AAPL,MSFT.")],
    hours: Annotated[float, typer.Option("--hours", min=0.0, help="How far back.")] = 24.0,
) -> None:
    """Dev: recent Alpaca news headlines for some symbols (REST)."""
    _run(_news(ctx.obj, symbols, hours))


@app.command()
def filings(
    ctx: typer.Context,
    symbol: Annotated[str, typer.Option("--symbol", help="One ticker, e.g. AAPL.")],
    days: Annotated[int, typer.Option("--days", min=1, help="Filing-date lookback.")] = 5,
) -> None:
    """Dev: recent SEC filings of flagged forms for a symbol (EDGAR)."""
    _run(_filings(ctx.obj, symbol, days))


async def _news(root: Path, symbols: str, hours: float) -> None:
    from trader.runtime import open_runtime

    async with open_runtime(root) as rt:
        wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        since = rt.clock.now() - dt.timedelta(hours=hours)
        items = await rt.rest.news(start=since, symbols=wanted)
        for item in sorted(items, key=lambda i: i.created_at, reverse=True):
            tags = ",".join(item.symbols[:5])
            typer.echo(f"{item.created_at:%Y-%m-%d %H:%M} [{item.source}] {tags}: {item.headline}")
        typer.echo(f"{len(items)} articles in the last {hours:g} h")


async def _filings(root: Path, symbol: str, days: int) -> None:
    from trader.data.edgar import EdgarClient
    from trader.runtime import open_runtime

    async with (
        open_runtime(root) as rt,
        EdgarClient.from_config(rt.cfg, rt.clock, rt.journal) as sec,
    ):
        found = await sec.recent_filings(symbol, days)
    for filing in found:
        when = (
            f"{filing.filed_at:%Y-%m-%d %H:%M}"
            if filing.filed_at
            else f"{filing.filing_date} --:--"
        )
        typer.echo(f"{when} {filing.form:<8} {filing.items or '-':<14} {filing.url}")
    typer.echo(f"{len(found)} flagged filings for {symbol.upper()} in the last {days} days")


async def _stream(
    root: Path, symbols: str | None, minutes: float, feed: str | None, *, with_news: bool
) -> None:
    from collections import Counter
    from typing import cast

    from trader.core.calendar import CalendarRangeError, NotATradingDayError
    from trader.core.models import Trade
    from trader.data import ws
    from trader.data.alpaca_rest import Feed
    from trader.data.news import NewsHub
    from trader.data.stream import BarEvent, MarketDataHub, StreamFeed
    from trader.runtime import open_runtime

    async with open_runtime(root) as rt:
        chosen = feed or rt.cfg.settings.data.feed
        if chosen not in ("iex", "sip", "test"):
            raise ValueError(f"--feed must be iex, sip or test, not {chosen!r}")
        stream_feed = cast(StreamFeed, chosen)
        default = "FAKEPACA" if stream_feed == "test" else "SPY,QQQ"
        wanted = [s.strip().upper() for s in (symbols or default).split(",") if s.strip()]
        calendar = await rt.calendar()

        def market_open(now: dt.datetime) -> bool:
            try:
                session = calendar.session(now.date())
            except (CalendarRangeError, NotATradingDayError):
                return False
            return session.open <= now < session.close

        async def backfill(names: Any, start: dt.datetime, end: dt.datetime) -> Any:
            rest_feed: Feed = "sip" if stream_feed == "sip" else "iex"
            return await rt.rest.bars(names, "1Min", start, end, feed=rest_feed)

        key, secret = rt.cfg.data_credentials()
        if key is None or secret is None:  # AlpacaRest.from_config already refuses this
            raise ValueError("no market-data keys in .env")
        hub = MarketDataHub(
            key_id=key.get_secret_value(),
            secret_key=secret.get_secret_value(),
            feed=stream_feed,
            clock=rt.clock,
            symbol_limit=rt.cfg.settings.data.stream_symbol_limit,
            backfill=None if stream_feed == "test" else backfill,
            market_open=market_open,
            connector=ws.websockets_connector,
        )
        news_hub: NewsHub | None = None
        if with_news:
            news_hub = NewsHub(
                rest=rt.rest,
                journal=rt.journal,
                clock=rt.clock,
                key_id=key.get_secret_value(),
                secret_key=secret.get_secret_value(),
                connector=ws.websockets_connector,
                on_item=lambda item: typer.echo(
                    f"news {item.created_at:%H:%M} {','.join(item.symbols[:5])}: {item.headline}"
                ),
            )
        events = hub.subscribe_consumer()
        await hub.start()
        if news_hub is not None:
            await news_hub.start(lookback=dt.timedelta(0))
        bars = trades_total = 0
        try:
            if not await hub.wait_connected(15.0):
                raise ValueError(f"stream did not connect: {hub.failed or 'timed out'}")
            acked = await hub.set_subscriptions(wanted)
            typer.echo(
                "subscribed: " + "; ".join(f"{k} {','.join(v)}" for k, v in sorted(acked.items()))
            )
            per_minute: Counter[str] = Counter()
            deadline = rt.clock.now() + dt.timedelta(minutes=minutes)
            while (remaining := (deadline - rt.clock.now()).total_seconds()) > 0:
                try:
                    event = await asyncio.wait_for(events.get(), timeout=min(remaining, 1.0))
                except TimeoutError:
                    continue
                if isinstance(event, Trade):
                    per_minute[event.symbol] += 1
                    trades_total += 1
                elif isinstance(event, BarEvent):
                    bars += 1
                    b = event.bar
                    note = "" if event.source == "stream" else f" ({event.source})"
                    typer.echo(
                        f"{b.ts:%H:%M} {event.symbol:<8} O {b.open} H {b.high} L {b.low} "
                        f"C {b.close} V {b.volume}  trades {per_minute.pop(event.symbol, 0)}{note}"
                    )
        finally:
            if news_hub is not None:
                await news_hub.stop()
            await hub.stop()
        typer.echo(f"done: {bars} bars, {trades_total} trades, {hub.connections} connection(s)")
        if news_hub is not None:
            typer.echo(f"news: {news_hub.mode} mode, {news_hub.stored} articles stored")


# ---------------------------------------------------------------- kill switch (RISK.md 6)

BookChoice = Annotated[str, typer.Option("--book", help="A, B or all.")]
FLATTEN_TIMEOUT_S = 60.0
FLATTENING_MARKER_TTL_S = 30.0


@app.command()
def halt(
    ctx: typer.Context,
    book: BookChoice = "all",
    flatten: Annotated[
        bool, typer.Option("--flatten", help="Also cancel every order and close every position.")
    ] = False,
) -> None:
    """Halt new entries for a book until `trader resume`. Works without the engine."""
    _run(_halt(ctx.obj, _books(book), flatten=flatten, source="trader halt"))


@app.command("flatten")
def flatten_command(ctx: typer.Context, book: BookChoice) -> None:
    """Halt the book, then cancel every order and close every position through the broker.
    Works without the engine. Outside market hours nothing is sold: positions keep GTC stops."""
    _run(_halt(ctx.obj, _books(book), flatten=True, source="trader flatten"))


@app.command()
def resume(
    ctx: typer.Context,
    book: Annotated[str, typer.Option("--book", help="A or B.")],
    ack_drawdown: Annotated[
        bool, typer.Option("--ack-drawdown", help="Acknowledge a drawdown halt (RISK.md 5).")
    ] = False,
) -> None:
    """Lift a book's halts after you type RESUME <book>. Refuses while runtime/KILL exists.
    Daily-loss and LLM halts still end only at the next session."""
    _run(_resume(ctx.obj, book, ack_drawdown=ack_drawdown))


def _books(choice: str) -> list[Literal["A", "B"]]:
    picked = choice.strip().upper()
    if picked == "ALL":
        return ["A", "B"]
    if picked == "A":
        return ["A"]
    if picked == "B":
        return ["B"]
    raise ValueError(f"--book must be A, B or all, not {choice!r}")


async def _halt(root: Path, books: list[Literal["A", "B"]], *, flatten: bool, source: str) -> None:
    from trader.core.calendar import CalendarRangeError, NotATradingDayError
    from trader.execution.alpaca_broker import AlpacaBroker
    from trader.execution.records import Records
    from trader.execution.service import MarketHours, flatten_account
    from trader.memory.incidents import record_incident
    from trader.risk.breakers import Halts
    from trader.runtime import open_local, open_runtime

    if not flatten:
        async with open_local(root) as local:
            halts = Halts(local.journal, local.clock)
            for book in books:
                await halts.halt(book, "manual", detail=source)
                typer.echo(f"Book {book}: halted. `trader resume --book {book}` lifts it.")
        return
    async with open_runtime(root) as rt:
        halts = Halts(rt.journal, rt.clock)
        for book in books:
            await halts.halt(book, "manual", detail=source)
        calendar = await rt.calendar()
        hours: MarketHours | None
        try:
            session = calendar.session(rt.clock.now().date())
            hours = (session.open, session.close)
        except (CalendarRangeError, NotATradingDayError):
            hours = None
        failed: list[str] = []
        marker = dt.timedelta(seconds=FLATTENING_MARKER_TTL_S)
        for book in books:
            records = Records(
                rt.journal, book=book, calendar=calendar, fees=rt.cfg.settings.fees, clock=rt.clock
            )
            broker = AlpacaBroker.from_config(rt.cfg, book, rt.clock)

            async def beat(book: Literal["A", "B"] = book) -> None:
                # Tells a running engine to leave this book's stops alone while we work.
                await halts.renew_marker(book, "flattening", marker)

            try:
                result = await flatten_account(
                    broker,
                    records,
                    clock=rt.clock,
                    deadline=rt.clock.now() + dt.timedelta(seconds=FLATTEN_TIMEOUT_S),
                    hours=hours,
                    heartbeat=beat,
                )
            finally:
                await halts.clear(book, ["flattening"], by=source)
                await broker.aclose()
            if result.flat:
                typer.echo(f"Book {book}: flat and halted.")
                continue
            held = ", ".join(f"{s} {q}" for s, q in sorted(result.remaining.items()))
            if result.market_closed:
                stops = ", ".join(f"{s} @ {p}" for s, p in sorted(result.protected.items()))
                typer.echo(f"Book {book}: market closed, nothing sold. Still held: {held}. "
                           f"GTC stops: {stops or 'none placed'}.")  # fmt: skip
            else:
                typer.echo(f"Book {book}: NOT flat after {FLATTEN_TIMEOUT_S:.0f} s: {held}.")
            failed.append(book)
            await record_incident(
                rt.journal,
                at=rt.clock.now(),
                severity="P1",
                component="flatten",
                code="not_flat",
                message=f"Book {book} not flat after {source}: {held}",
                detail={"remaining": result.remaining, "protected": result.protected},
            )
        if failed:
            raise typer.Exit(code=1)


async def _resume(root: Path, choice: str, *, ack_drawdown: bool) -> None:
    from trader.risk.breakers import Halts
    from trader.risk.killswitch import KILL_FILE
    from trader.runtime import open_local

    books = _books(choice)
    if len(books) != 1:
        raise ValueError("resume one book at a time: --book A or --book B")
    book = books[0]
    if (root / KILL_FILE).exists():
        raise ValueError(
            f"{KILL_FILE.as_posix()} exists. Delete it first; deleting it alone doesn't resume."
        )
    async with open_local(root) as local:
        halts = Halts(local.journal, local.clock)
        active = await halts.active(book)
        lasting = [h for h in active if h.scope == "until_resume"]
        for h in active:
            note = "lifted by resume" if h.scope == "until_resume" else f"ends {h.expires_at}"
            since = f"{h.created_at:%Y-%m-%d %H:%M}"
            typer.echo(f"  {h.reason:<18} since {since}  {h.detail}  ({note})")
        if not lasting:
            typer.echo(f"Book {book} has no halt that resume lifts.")
            return
        if any(h.reason == "drawdown" for h in lasting) and not ack_drawdown:
            raise ValueError("a drawdown halt needs --ack-drawdown after you review the drawdown")
        typed = typer.prompt(f"Type RESUME {book} to lift these halts")
        if typed.strip() != f"RESUME {book}":
            raise ValueError("confirmation did not match; nothing changed")
        cleared = await halts.resume(book, by="trader resume", ack_drawdown=ack_drawdown)
        typer.echo(f"Book {book}: lifted {', '.join(h.reason for h in cleared)}.")


# ---------------------------------------------------------------- smoke (HUMAN GATE G2)


@smoke_app.command("paper")
def smoke_paper(
    ctx: typer.Context,
    marketable: Annotated[
        bool, typer.Option("--marketable", help="Buy 1 share (paper), then flatten it.")
    ] = False,
    symbol: Annotated[str, typer.Option("--symbol", help="A liquid stock under $50.")] = "F",
) -> None:
    """HUMAN GATE G2: a 1-share OTO in Book A's paper account, through the risk gate. By
    default it rests and is cancelled; --marketable fills and flattens. Prints the journal."""
    _run(_smoke_paper(ctx.obj, symbol=symbol.strip().upper(), marketable=marketable))


async def _smoke_paper(root: Path, *, symbol: str, marketable: bool) -> None:
    from trader.core.calendar import day_times
    from trader.core.clock import ET
    from trader.core.events import EVENTS_FILE, load_events
    from trader.execution.alpaca_broker import AlpacaBroker
    from trader.execution.records import Records
    from trader.execution.service import ExecutionService, TradingDay
    from trader.execution.smoke import NoPrices, SmokeDeps, ensure_seeded, run_smoke
    from trader.risk.breakers import Halts
    from trader.risk.gate import RiskGate
    from trader.risk.killswitch import KILL_FILE
    from trader.risk.ledger import SettlementLedger
    from trader.runtime import open_runtime

    async with open_runtime(root) as rt:
        cfg, clock = rt.cfg, rt.clock
        calendar = await rt.calendar()
        today = clock.now().date()
        session = calendar.session(today)  # not a trading day: a one-line error
        times = day_times(
            session,
            entry_window_start=cfg.risk.entry_window_start,
            last_entry=cfg.settings.schedule.last_entry,
            flatten=cfg.settings.schedule.flatten,
        )
        day = TradingDay(times=times, next_open=calendar.session(calendar.next_session(today)).open)
        midnight = dt.datetime.combine(today, dt.time(0), ET)
        events = load_events(root / EVENTS_FILE).between(midnight, midnight + dt.timedelta(days=1))
        broker = AlpacaBroker.from_config(cfg, "A", clock)
        try:
            records = Records(
                rt.journal, book="A", calendar=calendar, fees=cfg.settings.fees, clock=clock
            )
            halts = Halts(rt.journal, clock)
            await ensure_seeded(SettlementLedger(rt.journal, "A", clock), broker, typer.echo)
            service = ExecutionService(
                book="A",
                broker=broker,
                journal=rt.journal,
                records=records,
                halts=halts,
                gate=RiskGate(cfg.risk, cfg.settings.strategy.universe),
                prices=NoPrices(),
                clock=clock,
                risk=cfg.risk,
                day=day,
            )
            await service.start()
            try:
                deps = SmokeDeps(
                    service=service,
                    broker=broker,
                    records=records,
                    halts=halts,
                    journal=rt.journal,
                    clock=clock,
                    market=rt.rest,
                    day=day,
                    mode=cfg.settings.mode,
                    feed=cfg.settings.data.feed,
                    events=tuple(events),
                    kill_file=root / KILL_FILE,
                )
                ok = await run_smoke(deps, symbol=symbol, marketable=marketable, echo=typer.echo)
            finally:
                await service.stop()
        finally:
            await broker.aclose()
    if not ok:
        raise typer.Exit(code=1)


def _run(job: Coroutine[Any, Any, None]) -> None:
    """Run an async command; expected failures print one line and exit 1, never a traceback."""
    from trader.config import ConfigError
    from trader.core.calendar import CalendarRangeError, NotATradingDayError
    from trader.data.alpaca_rest import AlpacaError
    from trader.data.edgar import EdgarError
    from trader.execution.broker import BrokerError
    from trader.risk.breakers import HaltError
    from trader.risk.ledger import LedgerError

    try:
        asyncio.run(job)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except (
        AlpacaError,
        BrokerError,
        EdgarError,
        CalendarRangeError,
        NotATradingDayError,
        HaltError,
        LedgerError,
        ValueError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
