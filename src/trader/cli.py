"""`trader` command-line interface."""

import asyncio
import datetime as dt
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any

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


def _run(job: Coroutine[Any, Any, None]) -> None:
    """Run an async command; expected failures print one line and exit 1, never a traceback."""
    from trader.config import ConfigError
    from trader.core.calendar import CalendarRangeError, NotATradingDayError
    from trader.data.alpaca_rest import AlpacaError
    from trader.data.edgar import EdgarError

    try:
        asyncio.run(job)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None
    except (AlpacaError, EdgarError, CalendarRangeError, NotATradingDayError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
