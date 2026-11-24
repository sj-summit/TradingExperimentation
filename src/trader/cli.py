"""`trader` command-line interface."""

import asyncio
import datetime as dt
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import typer

from trader import __version__

if TYPE_CHECKING:
    from trader.config import AppConfig
    from trader.ops.schedule_cmds import ScheduleDeps

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
report_app = typer.Typer(help="Reports (ARCHITECTURE.md 4.10).", no_args_is_help=True)
app.add_typer(report_app, name="report")
notify_app = typer.Typer(help="Push notifications (ntfy or Discord).", no_args_is_help=True)
app.add_typer(notify_app, name="notify")
followups_app = typer.Typer(
    help="Claude's process issues and notes from its reviews; only you close them "
    "(STRATEGY.md 6.6).",
    no_args_is_help=True,
)
app.add_typer(followups_app, name="followups")
epoch_app = typer.Typer(
    help="The evaluation epoch: which one is current, and starting the next; only you start "
    "one (STRATEGY.md 9).",
    no_args_is_help=True,
)
app.add_typer(epoch_app, name="epoch")
lessons_app = typer.Typer(
    help="Claude's lesson proposals, tested by code on later signals (STRATEGY.md 6.2-6.3).",
    no_args_is_help=True,
)
app.add_typer(lessons_app, name="lessons")
schedule_app = typer.Typer(
    help="Windows Task Scheduler entries for unattended runs (PLAN P6.1a). Installing them is "
    "HUMAN GATE G6a.",
    no_args_is_help=True,
)
app.add_typer(schedule_app, name="schedule")

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
    runtime dirs, the PC's sleep settings, notifications, token age, and events.yaml coverage.
    Exits 1 on any FAIL."""
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


@app.command("run-day")
def run_day(
    ctx: typer.Context,
    books: Annotated[
        str, typer.Option("--books", help="A, B or A,B: Book B adds Claude's veto.")
    ] = "A",
    sim: Annotated[
        bool,
        typer.Option(
            "--sim",
            help="Live data, simulated brokers and a scripted model, under runtime/sim/<date>/.",
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Boot's checks and the timeline; trades nothing.")
    ] = False,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Book B off for the day: Book A runs alone.")
    ] = False,
    scheduled: Annotated[
        bool,
        typer.Option(
            "--scheduled", hidden=True, help="Set by the OS scheduler's task (trader schedule)."
        ),
    ] = False,
) -> None:
    """HUMAN GATE until Phase 6: today's full session (A§3) for the books' paper accounts, from
    boot to the report. Start it by 08:25 ET; it exits after the 16:30 report."""
    _run(_run_day(ctx.obj, books, sim=sim, dry_run=dry_run, no_llm=no_llm, scheduled=scheduled))


async def _run_day(
    root: Path, books: str, *, sim: bool, dry_run: bool, no_llm: bool, scheduled: bool
) -> None:
    from trader.orchestrator.live import RunOptions, parse_books, run_day

    options = RunOptions(
        books=parse_books(books), sim=sim, dry_run=dry_run, no_llm=no_llm, scheduled=scheduled
    )
    await run_day(root, options, typer.echo)


@app.command()
def replay(
    ctx: typer.Context,
    date: Annotated[str, typer.Option("--date", help="A recorded session, YYYY-MM-DD.")],
) -> None:
    """Replay a recorded day offline through the real engine (recorded market data and broker
    answers on a simulated clock) into runtime/replay/<date>/, then compare its signals,
    decisions and trades with the journal. Exits 1 on any divergence or difference. Never
    touches an account or sends a notification."""
    _run(_replay(ctx.obj, date))


async def _replay(root: Path, date: str) -> None:
    from trader.config import load_config
    from trader.core.clock import ET, RealClock
    from trader.core.events import EVENTS_FILE, load_events
    from trader.core.logging import configure_logging, shutdown_logging
    from trader.orchestrator.replay import replay_day

    cfg = load_config(root)
    day = dt.date.fromisoformat(date)
    midnight = dt.datetime.combine(day, dt.time(0), ET)
    events = load_events(root / EVENTS_FILE).between(midnight, midnight + dt.timedelta(days=1))
    db = cfg.path(cfg.settings.storage.db_path)
    replays = db.parent / "replay"
    configure_logging(
        clock=RealClock(),
        secrets=cfg.secrets.redaction_map(),
        logs_dir=replays / "logs",
        console=None,
    )
    try:
        report = await replay_day(
            recordings=cfg.path(cfg.settings.storage.recordings_dir),
            day=day,
            out=replays / day.isoformat(),
            settings=cfg.settings,
            risk=cfg.risk,
            events=events,
            recorded_db=db,
            root=root,
        )
    finally:
        shutdown_logging()
    for line in report.lines():
        typer.echo(line)
    if not report.matches:
        raise typer.Exit(code=1)


@app.command()
def scan(
    ctx: typer.Context,
    date: Annotated[
        str | None,
        typer.Option(
            "--date",
            help="Session YYYY-MM-DD. Default: the latest session whose 09:35 scan has passed.",
        ),
    ] = None,
) -> None:
    """Dev: run the 09:35 opening-range scan for a session and print the top rows. Saves
    nothing. Needs that session's universe (`trader universe build --date D`)."""
    _run(_scan(ctx.obj, date))


async def _scan(root: Path, date: str | None) -> None:
    from trader.runtime import open_runtime
    from trader.strategy.orb_sip import OrbSipSelector, last_scanned_session, scan_lines

    async with open_runtime(root) as rt:
        calendar = await rt.calendar()
        settings = rt.cfg.settings
        scan_at = settings.strategy.selection.scan_at
        day = (
            dt.date.fromisoformat(date)
            if date
            else last_scanned_session(calendar, rt.clock.now(), scan_at)
        )
        selector = OrbSipSelector(
            market=rt.rest,
            journal=rt.journal,
            clock=rt.clock,
            calendar=calendar,
            strategy=settings.strategy,
            max_chase_pct=rt.cfg.risk.max_chase_pct,
            feed=settings.data.feed,
            ready_symbol=settings.data.regime_symbols[0],
        )
        result = await selector.scan(day, persist=False)
        for line in scan_lines(result, settings.data.feed):
            typer.echo(line)


@report_app.command("daily")
def report_daily(
    ctx: typer.Context,
    date: Annotated[
        str | None, typer.Option("--date", help="Session YYYY-MM-DD. Default: today.")
    ] = None,
) -> None:
    """Regenerate a day's report (Markdown and HTML) from the journal. Offline."""
    _run(_report_daily(ctx.obj, date))


async def _report_daily(root: Path, date: str | None) -> None:
    from trader.core.calendar import CalendarRangeError, NotATradingDayError, load_calendar
    from trader.reports.daily import build_daily, write_daily
    from trader.runtime import open_local

    async with open_local(root) as local:
        now = local.clock.now()
        day = dt.date.fromisoformat(date) if date else now.date()
        try:
            session = (await load_calendar(local.journal)).session(day)
        except (CalendarRangeError, NotATradingDayError):
            session = None
        rows = await local.journal.fetch_all(
            "SELECT DISTINCT book FROM daily_stats WHERE date = ? ORDER BY book", (day.isoformat(),)
        )
        books = tuple(r["book"] for r in rows) or ("A",)
        report = await build_daily(
            local.journal,
            day,
            books=books,
            mode=local.cfg.settings.mode,
            session=session,
            now=now,
            max_calls=local.cfg.settings.llm.max_calls_per_day,
            degrade_after=local.cfg.risk.llm_failures_degrade_after,
        )
        md, page = write_daily(report, local.cfg.path(local.cfg.settings.storage.reports_dir))
        typer.echo(report.summary())
        typer.echo(f"report: {md}\n        {page}")


@report_app.command("weekly")
def report_weekly(
    ctx: typer.Context,
    week: Annotated[
        str | None,
        typer.Option(
            "--week", help="ISO week YYYY-Www, e.g. 2026-W47. Default: the last one done."
        ),
    ] = None,
    offline: Annotated[
        bool, typer.Option("--offline", help="Don't fetch SPY's missing daily closes.")
    ] = False,
) -> None:
    """The week's report (Markdown and HTML), with the epoch to date. First fetches the
    week's missing SPY closes (one SIP request a session), unless --offline."""
    _run(_report_weekly(ctx.obj, week, offline=offline))


async def _report_weekly(root: Path, week: str | None, *, offline: bool) -> None:
    import re

    from trader.reports.weekly import fill_spy_closes, iso_week, last_complete_week
    from trader.runtime import open_local, open_runtime

    picked: tuple[int, int] | None = None
    if week is not None:
        found = re.fullmatch(r"(\d{4})-W(\d{2})", week.strip())
        if found is None:
            typer.echo(f"--week takes YYYY-Www, like 2026-W47, not {week!r}")
            raise typer.Exit(1)
        picked = (int(found[1]), int(found[2]))
    if offline:
        async with open_local(root) as local:
            await _write_weekly(local, picked or last_complete_week(local.clock.now().date()))
        return
    async with open_runtime(root) as rt:
        chosen = picked or last_complete_week(rt.clock.now().date())
        first, last = iso_week(*chosen)
        rows = await rt.journal.fetch_all(
            "SELECT DISTINCT date FROM daily_stats WHERE date BETWEEN ? AND ? ORDER BY date",
            (first.isoformat(), last.isoformat()),
        )
        sessions = [dt.date.fromisoformat(r["date"]) for r in rows]
        missing = await fill_spy_closes(rt.journal, rt.rest, rt.clock, sessions)
        if missing:
            typer.echo(f"SPY's close still missing for {', '.join(map(str, missing))}")
        await _write_weekly(rt, chosen)


@report_app.command("evaluation")
def report_evaluation(
    ctx: typer.Context,
    epoch: Annotated[
        int | None, typer.Option("--epoch", help="The epoch to judge. Default: the current one.")
    ] = None,
    through: Annotated[
        str | None, typer.Option("--through", help="Its last session YYYY-MM-DD. Default: today.")
    ] = None,
) -> None:
    """The pre-registered evaluation memo (STRATEGY.md 8): every criterion pass, fail or
    insufficient data, and the outcome. Nothing here trades live. Offline."""
    _run(_report_evaluation(ctx.obj, epoch, through))


async def _report_evaluation(root: Path, epoch: int | None, through: str | None) -> None:
    from trader.reports.evaluation import build_memo, counts, write_memo
    from trader.runtime import open_local

    try:
        picked = dt.date.fromisoformat(through.strip()) if through is not None else None
    except ValueError:
        typer.echo(f"--through takes YYYY-MM-DD, like 2026-11-20, not {through!r}")
        raise typer.Exit(1) from None
    async with open_local(root) as local:
        last = picked or local.clock.now().date()
        cfg = local.cfg.settings.lessons
        memo = await local.journal.read(
            lambda conn: build_memo(conn, through=last, cfg=cfg, epoch=epoch)
        )
        md, page = write_memo(memo, local.cfg.path(local.cfg.settings.storage.reports_dir))
    typer.echo(f"epoch {memo.epoch} through {memo.through}: {memo.outcome} ({counts(memo)})")
    typer.echo(f"report: {md}\n        {page}")


async def _write_weekly(local: Any, chosen: tuple[int, int]) -> None:
    from trader.memory.stats import Bootstrap
    from trader.reports.weekly import build_weekly, write_weekly

    boot = Bootstrap.of(local.cfg.settings.lessons)
    year, week = chosen
    report = await local.journal.read(
        lambda conn: build_weekly(conn, year=year, week=week, boot=boot)
    )
    md, page = write_weekly(report, local.cfg.path(local.cfg.settings.storage.reports_dir))
    v = report.epoch_stats.veto
    value = "-" if v.v is None else f"{v.v:+.3f}"
    typer.echo(
        f"week {year}-W{week:02d}: {len(report.curve)} sessions in epoch {report.epoch}; "
        f"V {value} (n {v.n_allowed}/{v.n_vetoed})"
    )
    typer.echo(f"report: {md}\n        {page}")


@followups_app.command("list")
def followups_list(
    ctx: typer.Context,
    include_closed: Annotated[bool, typer.Option("--all", help="Closed ones too.")] = False,
) -> None:
    """Claude's open process issues and notes, the oldest and most recurring first. Offline."""
    _run(_followups_list(ctx.obj, include_closed=include_closed))


@followups_app.command("set")
def followups_set(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="issue or note.")],
    item_id: Annotated[int, typer.Argument(help="Its id, as `followups list` shows it.")],
    status: Annotated[
        str,
        typer.Option(
            "--status", help="An issue: fixed or wontfix. A note: accepted, rejected or done."
        ),
    ],
    response: Annotated[
        str, typer.Option("--response", help="Why, for the record: 'fixed in abc123'.")
    ] = "",
) -> None:
    """Close or answer one of Claude's process issues or notes. Offline."""
    _run(_followups_set(ctx.obj, kind, item_id, status, response))


async def _followups_list(root: Path, *, include_closed: bool) -> None:
    from trader.memory.reviews import followups
    from trader.runtime import open_local

    async with open_local(root) as local:
        items = await local.journal.read(
            lambda conn: followups(conn, include_closed=include_closed)
        )
    if not items:
        typer.echo("No open process issues or notes." if not include_closed else "None yet.")
        return
    for f in items:
        about = f"{_printable(f.component)}: " if f.component else ""
        line = (
            f"{f.kind:<5} {f.id:>4}  {f.status:<8} {f.severity:<5}  {f.first}..{f.last}  "
            f"x{f.count}  {about}{_printable(f.text)}"
        )
        typer.echo(line + (f"  [{_printable(f.response)}]" if f.response else ""))


async def _followups_set(root: Path, kind: str, item_id: int, status: str, response: str) -> None:
    from trader.memory.reviews import FollowupError, FollowupKind, set_followup
    from trader.runtime import open_local

    picked: FollowupKind
    if kind.strip().lower() == "issue":
        picked = "issue"
    elif kind.strip().lower() == "note":
        picked = "note"
    else:
        typer.echo(f"the kind is issue or note, not {kind!r}")
        raise typer.Exit(1)
    wanted = status.strip().lower()
    async with open_local(root) as local:
        now = local.clock.now()
        try:
            await local.journal.write(
                lambda conn: set_followup(conn, picked, item_id, wanted, response, now)
            )
        except FollowupError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1) from exc
    typer.echo(f"{picked} {item_id}: {wanted}")


def _printable(text: str) -> str:
    """A model's text for the terminal: no control characters or escape sequences."""
    return "".join(ch if ch.isprintable() else " " for ch in text)


@lessons_app.command("evaluate")
def lessons_evaluate(
    ctx: typer.Context,
    promote: Annotated[
        bool, typer.Option("--promote", help="Store the outcomes: promote, reject or demote.")
    ] = False,
) -> None:
    """Test every proposed and promoted lesson on the current epoch's signals after the day that
    proposed it. Without --promote nothing is written. Offline."""
    _run(_lessons_evaluate(ctx.obj, promote=promote))


@lessons_app.command("list")
def lessons_list(ctx: typer.Context) -> None:
    """Every lesson, with its status, numbers and statement. Offline."""
    _run(_lessons_list(ctx.obj))


async def _lessons_evaluate(root: Path, *, promote: bool) -> None:
    from trader.memory.lessons import evaluate_lessons
    from trader.memory.playbook import PLAYBOOK_FILE, playbook, write_playbook
    from trader.runtime import open_local

    async with open_local(root) as local:
        now, cfg = local.clock.now(), local.cfg.settings.lessons
        outcomes = await (local.journal.write if promote else local.journal.read)(
            lambda conn: evaluate_lessons(conn, now=now, cfg=cfg, write=promote)
        )
        if promote:
            entries = await local.journal.read(lambda conn: playbook(conn, cfg))
            runtime = local.cfg.path(local.cfg.settings.storage.db_path).parent
            written = write_playbook(runtime / PLAYBOOK_FILE, entries, as_of=now)
    if not outcomes:
        typer.echo("No proposed or promoted lessons.")
    for o in outcomes:
        typer.echo(f"{o.lesson.id:<6} {o.lesson.status:>8} -> {o.status:<8} "
                   f"{_numbers(o.evaluation)}  {o.reason}")  # fmt: skip
    if promote:
        typer.echo(f"playbook: {len(entries)} lessons in {written}")
    else:
        typer.echo("nothing written (--promote stores the outcomes)")


async def _lessons_list(root: Path) -> None:
    from trader.memory.lessons import read_lessons
    from trader.runtime import open_local

    async with open_local(root) as local:
        rows = await local.journal.fetch_all("SELECT * FROM lessons ORDER BY id")
        lessons = {x.id: x for x in await local.journal.read(read_lessons)}
    if not rows:
        typer.echo("No lessons yet.")
    for r in rows:
        ci = "" if r["ci_low"] is None else f" CI [{r['ci_low']:+.2f}, {r['ci_high']:+.2f}]"
        effect = "" if r["effect_r"] is None else f" effect {r['effect_r']:+.2f}R"
        n = "" if r["n_subset"] is None else f" n {r['n_subset']}/{r['n_complement']}"
        typer.echo(f"{r['id']:<6} {r['status']:<8}{n}{effect}{ci}  "
                   f"{_printable(lessons[r['id']].statement)}")  # fmt: skip


def _numbers(ev: Any) -> str:
    if ev is None:
        return "-"
    effect = "effect -" if ev.effect_r is None else f"effect {ev.effect_r:+.2f}R"
    i = ev.interval
    ci = "" if i is None else f" CI [{i.low:+.2f}, {i.high:+.2f}]"
    return f"n {ev.n_subset}/{ev.n_complement} {effect}{ci}"


@epoch_app.command("show")
def epoch_show(ctx: typer.Context) -> None:
    """The current epoch, and every epoch's record. Offline."""
    _run(_epoch_show(ctx.obj))


@epoch_app.command("start")
def epoch_start(
    ctx: typer.Context,
    epoch: Annotated[int, typer.Argument(help="The next epoch's number: the current one + 1.")],
    decision: Annotated[str, typer.Option("--decision", help="Its DECISIONS entry: D-080.")],
    reason: Annotated[str, typer.Option("--reason", help="Why it begins, for the record.")],
    note: Annotated[
        int | None, typer.Option("--note", help="The review note that led to it, if one did.")
    ] = None,
) -> None:
    """Start the next evaluation epoch: record it with the hashes of the settings, risk and
    events files and the LLM fingerprint, and make it current. Yours to run, never while a
    run-day is. Offline."""
    _run(_epoch_start(ctx.obj, epoch, decision=decision, reason=reason, note=note))


async def _epoch_show(root: Path) -> None:
    from trader.memory.epochs import current_epoch, epochs
    from trader.runtime import open_local

    async with open_local(root) as local:
        now, rows = await local.journal.read(lambda conn: (current_epoch(conn), epochs(conn)))
    typer.echo(f"current epoch: {now}")
    for e in rows:
        since = e.started_at[:10] if e.started_at else "the start"
        note = f", note {e.note_id}" if e.note_id is not None else ""
        typer.echo(f"  {e.epoch}  since {since}  {e.decision or '-'}{note}: {e.reason}")


async def _epoch_start(
    root: Path, epoch: int, *, decision: str, reason: str, note: int | None
) -> None:
    import hashlib

    from trader.memory.epochs import EpochError, start_epoch
    from trader.orchestrator.lock import LOCK_NAME, RunLock
    from trader.runtime import open_local

    async with open_local(root) as local:
        cfg = local.cfg
        files = {"settings": "settings.yaml", "risk": "risk.yaml", "events": "events.yaml"}
        hashes = {
            name: hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            for name, path in ((n, cfg.root / "config" / f) for n, f in files.items())
        }
        now = local.clock.now()
        # the epoch never changes in the middle of a day: run-day stamps its rows with it
        with RunLock(cfg.path(cfg.settings.storage.db_path).parent / LOCK_NAME):
            try:
                row = await local.journal.write(
                    lambda conn: start_epoch(conn, epoch=epoch, at=now, reason=reason,
                                             decision=decision, note_id=note, hashes=hashes)
                )  # fmt: skip
            except EpochError as exc:
                typer.echo(str(exc))
                raise typer.Exit(1) from exc
    typer.echo(f"epoch {row.epoch} begins ({row.decision}): {row.reason}")


@notify_app.command("test")
def notify_test(ctx: typer.Context) -> None:
    """Send one test message through the configured provider, to check your channel (H0.5)."""
    _run(_notify_test(ctx.obj))


async def _notify_test(root: Path) -> None:
    from trader.notify import NullNotifier, notifier_from_config
    from trader.runtime import open_local

    async with open_local(root) as local:
        notifier = notifier_from_config(local.cfg)
        if isinstance(notifier, NullNotifier):
            typer.echo("notify.provider is none, or its secret is missing: nothing sent")
            return
        sent = await notifier.send(
            "trader notify test", "If you can read this, alerts reach you.", tags=("test_tube",)
        )
        if not sent:
            raise ValueError("the provider refused the test message; see the log")
        typer.echo(f"sent through {local.cfg.settings.notify.provider}")


@app.command()
def backtest(
    ctx: typer.Context,
    start: Annotated[str, typer.Option("--start", help="First session, YYYY-MM-DD.")],
    end: Annotated[str, typer.Option("--end", help="Last session, YYYY-MM-DD, before today.")],
    feed: Annotated[
        str | None,
        typer.Option(
            "--feed", help="iex (mirrors live) or sip (as in the paper). Default: settings."
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Run between 09:00 and 16:30 ET on a trading day.")
    ] = False,
    tape: Annotated[
        bool,
        typer.Option(
            "--tape",
            help="Fetch the SIP trade tape of each ambiguous trigger minute not yet cached "
            "(one request each; D-057). Without it, cached tapes are used and the rest fall back.",
        ),
    ] = False,
) -> None:
    """Rules-only ORB backtest on historical data (P3.9). Never uses the LLM (D-010). Data is
    cached under runtime/backtest/, so a rerun over the same range makes no requests."""
    _run(_backtest(ctx.obj, start, end, feed, force=force, tape=tape))


async def _backtest(
    root: Path, start: str, end: str, feed: str | None, *, force: bool, tape: bool = False
) -> None:
    from typing import cast

    from trader.backtest import report
    from trader.backtest.data import BacktestData
    from trader.backtest.engine import BacktestSettings, DayResult, busy_hours, run_backtest
    from trader.data.alpaca_rest import Feed
    from trader.memory.journal import Journal
    from trader.runtime import open_runtime

    async with open_runtime(root) as rt:
        calendar = await rt.calendar()
        now = rt.clock.now()
        first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        chosen = feed or rt.cfg.settings.data.feed
        if chosen not in ("iex", "sip"):
            raise ValueError(f"--feed must be iex or sip, not {chosen!r}")
        if not first <= last < now.date():
            raise ValueError("--start must not be after --end, and --end must be before today")
        if busy_hours(calendar, now) and not force:
            raise ValueError(
                "the backtest doesn't run 09:00-16:30 ET on trading days: it would share the "
                "market-data rate limit with the live scan (--force overrides)"
            )

        def progress(n: int, total: int, day: DayResult) -> None:
            typer.echo(
                f"{n}/{total} {day.day}: {day.eligible} eligible, {len(day.signals)} signals, "
                f"{day.triggered} triggered, {len(day.trades)} trades, cash ${day.end_cash:.2f}"
            )

        cache_dir = rt.cfg.path(rt.cfg.settings.storage.db_path).parent / "backtest"
        async with await Journal.open(cache_dir / "cache.db") as cache:
            data = BacktestData(source=rt.rest, cache=cache, calendar=calendar)
            result = await run_backtest(
                data,
                calendar=calendar,
                settings=rt.cfg.settings,
                risk=rt.cfg.risk,
                cfg=BacktestSettings(start=first, end=last, feed=cast(Feed, chosen), tape=tape),
                progress=progress,
            )
        reports = rt.cfg.path(rt.cfg.settings.storage.reports_dir)
        md, table = report.write(result, reports, cache_dir)
        typer.echo(f"report: {md}\nsignals: {table}")


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


@app.command()
def fees(
    ctx: typer.Context,
    book: Annotated[str, typer.Option("--book", help="A, B or all.")] = "A",
    days: Annotated[int, typer.Option("--days", min=1, help="How far back.")] = 5,
) -> None:
    """Read-only: the fees Alpaca charged a book's account (FEE activities, D-055), then the
    account's cash now. Places and cancels nothing."""
    _run(_fees(ctx.obj, _books(book), days))


async def _fees(root: Path, books: list[Literal["A", "B"]], days: int) -> None:
    from collections import defaultdict
    from decimal import Decimal

    from trader.execution.alpaca_broker import AlpacaBroker
    from trader.runtime import open_local

    async with open_local(root) as local:
        since = (local.clock.now() - dt.timedelta(days=days)).replace(microsecond=0)
        for book in books:
            broker = AlpacaBroker.from_config(local.cfg, book, local.clock)
            try:
                found = await broker.fee_activities(since)
                cash = (await broker.account()).cash
            finally:
                await broker.aclose()
            typer.echo(f"Book {book}: {len(found)} fee activities since {since:%Y-%m-%d %H:%M}")
            by_date: dict[dt.date, Decimal] = defaultdict(Decimal)
            for fee in found:
                made = f"{fee.created_at:%Y-%m-%d %H:%M}" if fee.created_at else "?"
                typer.echo(
                    f"  {fee.date} {fee.sub_type or '-':<4} {fee.net_amount:>8} {fee.status:<9} "
                    f"{fee.symbol or '-':<6} created {made}"
                )
                if fee.status != "canceled":
                    by_date[fee.date] += fee.net_amount
            for day, total in sorted(by_date.items()):
                typer.echo(f"  {day}: {total} in all")
            typer.echo(f"  cash now {cash}")


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


@app.command()
def watchdog(ctx: typer.Context) -> None:
    """The independent safety check (RISK.md 6). The scheduler runs it every minute from 09:25
    to 16:05 ET on weekdays; outside that window, or on a holiday, it exits at once. It flattens
    the books of an engine that stopped beating, and shares no stop protects."""
    _run(_watchdog(ctx.obj))


async def _watchdog(root: Path) -> None:
    from trader.config import load_config
    from trader.core.clock import RealClock
    from trader.core.logging import configure_logging, shutdown_logging
    from trader.execution.alpaca_broker import AlpacaBroker
    from trader.execution.broker import BrokerAdapter
    from trader.memory.journal import Journal
    from trader.notify import notifier_from_config
    from trader.orchestrator.heartbeat import heartbeat_path
    from trader.orchestrator.lock import WATCHDOG_LOCK_NAME, RunLock, RunLockError
    from trader.orchestrator.watchdog import STATUS_NAME, Watchdog, WatchdogDeps
    from trader.risk.killswitch import KILL_FILE

    cfg = load_config(root)
    clock = RealClock()
    db = cfg.path(cfg.settings.storage.db_path)
    lock = RunLock(db.parent / WATCHDOG_LOCK_NAME, holder="`trader watchdog`")
    try:
        lock.acquire()
    except RunLockError:
        typer.echo("another watchdog run is still going: this one stands down")
        return
    logs = cfg.path(cfg.settings.storage.logs_dir)
    configure_logging(  # its own file: ~390 runs a day stay out of the engine's (D-082)
        clock=clock, secrets=cfg.secrets.redaction_map(), logs_dir=logs, prefix="watchdog-"
    )
    try:
        async with await Journal.open(db) as journal:
            calendar = await _today_calendar(cfg, clock, journal)
            brokers: dict[Literal["A", "B"], BrokerAdapter] = {
                book: AlpacaBroker.from_config(cfg, book, clock)
                for book in ("A", "B")
                if all(cfg.alpaca_credentials(book))
            }
            try:
                report = await Watchdog(
                    WatchdogDeps(
                        clock=clock,
                        journal=journal,
                        calendar=calendar,
                        brokers=brokers,
                        notifier=notifier_from_config(cfg),
                        settings=cfg.settings,
                        risk=cfg.risk,
                        heartbeat=heartbeat_path(db),
                        status=db.parent / STATUS_NAME,
                        kill_file=root / KILL_FILE,
                    )
                ).run()
            finally:
                for broker in brokers.values():
                    await broker.aclose()
        typer.echo(report.line())
    finally:
        shutdown_logging()
        lock.release()


async def _today_calendar(cfg: "AppConfig", clock: Any, journal: Any) -> Any:
    """The journal's calendar, synced from Alpaca (one request) only when it doesn't cover
    today, or was never synced."""
    from trader.core.calendar import (
        CalendarRangeError,
        default_sync_range,
        load_calendar,
        sync_calendar,
    )
    from trader.data.alpaca_rest import AlpacaRest

    today = clock.now().date()
    try:
        calendar = await load_calendar(journal)
    except CalendarRangeError:
        calendar = None
    if calendar is not None and calendar.covers(today):
        return calendar
    async with AlpacaRest.from_config(cfg, clock) as rest:
        first, last = default_sync_range(today)
        synced, _ = await sync_calendar(rest.calendar, journal, first, last)
    return synced


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
    from trader.execution.alpaca_broker import AlpacaBroker
    from trader.execution.broker import BrokerAdapter, BrokerError
    from trader.execution.flatten import flatten_books
    from trader.memory.incidents import record_incident
    from trader.memory.unattended import note_human
    from trader.risk.breakers import Halts
    from trader.runtime import open_local, open_runtime

    if not flatten:
        async with open_local(root) as local:
            await note_human(local.journal, local.clock, source)  # not unattended (D-081)
            halts = Halts(local.journal, local.clock)
            for book in books:
                await halts.halt(book, "manual", detail=source)
                typer.echo(f"Book {book}: halted. `trader resume --book {book}` lifts it.")
        return
    async with open_runtime(root) as rt:
        await note_human(rt.journal, rt.clock, source)  # not unattended (D-081)
        halts = Halts(rt.journal, rt.clock)
        for book in books:  # first, before any network: the engine stops entering (R-005)
            await halts.halt(book, "manual", detail=source)
        failed: list[str] = []
        brokers: dict[Literal["A", "B"], BrokerAdapter] = {}
        for book in books:  # one book's missing keys don't stop the other's flatten
            try:
                brokers[book] = AlpacaBroker.from_config(rt.cfg, book, rt.clock)
            except BrokerError as exc:
                typer.echo(f"Book {book}: halted, but not flattened: {exc}")
                failed.append(book)
        try:
            calendar = await _today_calendar(rt.cfg, rt.clock, rt.journal)  # cached by run-day
            results = await flatten_books(
                brokers,
                journal=rt.journal,
                calendar=calendar,
                fees=rt.cfg.settings.fees,
                clock=rt.clock,
                halt_reason="manual",
                purpose="manual",
                source=source,
                timeout_s=FLATTEN_TIMEOUT_S,
            )
        finally:
            for broker in brokers.values():
                await broker.aclose()
        for book in brokers:
            result = results[book]
            if result is not None and result.flat:
                typer.echo(f"Book {book}: flat and halted.")
                continue
            failed.append(book)
            if result is None:
                typer.echo(f"Book {book}: the flatten failed; see the log.")
                held = "unknown"
            else:
                held = ", ".join(f"{s} {q}" for s, q in sorted(result.remaining.items()))
                if result.market_closed:
                    stops = ", ".join(f"{s} @ {p}" for s, p in sorted(result.protected.items()))
                    bare = ", ".join(f"{s} {q}" for s, q in sorted(result.unprotected.items()))
                    typer.echo(
                        f"Book {book}: market closed, nothing sold. Still held: {held}. "
                        f"GTC stops: {stops or 'none placed'}."
                        + (f" NO STOP on: {bare}." if bare else "")
                    )
                else:
                    typer.echo(f"Book {book}: NOT flat after {FLATTEN_TIMEOUT_S:.0f} s: {held}.")
            await record_incident(
                rt.journal,
                at=rt.clock.now(),
                severity="P1",
                component="flatten",
                code="not_flat",
                message=f"Book {book} not flat after {source}: {held}",
                detail=None
                if result is None
                else {
                    "remaining": result.remaining,
                    "protected": result.protected,
                    "unprotected": result.unprotected,
                },
            )
        if failed:
            raise typer.Exit(code=1)


async def _resume(root: Path, choice: str, *, ack_drawdown: bool) -> None:
    from trader.memory.unattended import note_human
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
        await note_human(local.journal, local.clock, "trader resume")  # D-081
        typer.echo(f"Book {book}: lifted {', '.join(h.reason for h in cleared)}.")


# ---------------------------------------------------------------- the schedule (Phase 6)

LogonOption = Annotated[
    str | None,
    typer.Option(
        "--logon",
        help="s4u: whether you're logged on or not (install's default). interactive: only "
        "while you are.",
    ),
]
UniverseOption = Annotated[
    bool, typer.Option("--with-universe", help="Also build the next session's universe at 20:00.")
]


@schedule_app.command("show")
def schedule_show(
    ctx: typer.Context, logon: LogonOption = None, with_universe: UniverseOption = False
) -> None:
    """The tasks (ET and this PC's clock, command lines, logon), whether each is installed as
    shown, and anything that would make install refuse. Changes nothing."""
    from trader.ops import schedule_cmds

    cfg = _config(ctx.obj)
    code = schedule_cmds.show(
        ctx.obj, cfg, _schedule_deps(), logon=_logon(logon), with_universe=with_universe
    )
    raise typer.Exit(code)


@schedule_app.command("install")
def schedule_install(
    ctx: typer.Context, logon: LogonOption = None, with_universe: UniverseOption = False
) -> None:
    """HUMAN GATE G6a: register the tasks after you type INSTALL. Refuses while notifications
    are off (H0.5), or in a time zone the schedule can't follow."""
    from trader.ops import schedule_cmds

    cfg = _config(ctx.obj)
    code = schedule_cmds.install(
        ctx.obj, cfg, _schedule_deps(), logon=_logon(logon) or "s4u", with_universe=with_universe
    )
    raise typer.Exit(code)


@schedule_app.command("uninstall")
def schedule_uninstall(ctx: typer.Context) -> None:
    """Delete the ai-day-trader tasks, after a yes."""
    from trader.ops import schedule_cmds

    raise typer.Exit(schedule_cmds.uninstall(ctx.obj, _schedule_deps()))


@schedule_app.command("check")
def schedule_check(ctx: typer.Context, logon: LogonOption = None) -> None:
    """Prove the tasks can work where they run: a one-off task, as the installed ones run, runs
    doctor and a test push; its log is printed and the task deleted (G6a)."""
    from trader.ops import schedule_cmds

    raise typer.Exit(schedule_cmds.check(ctx.obj, _schedule_deps(), logon=_logon(logon)))


@schedule_app.command("probe")
def schedule_probe(ctx: typer.Context) -> None:
    """What `schedule check`'s task runs: doctor, then a test push. Exits 1 on any FAIL."""
    from trader.ops import schedule_cmds

    raise typer.Exit(schedule_cmds.probe(ctx.obj, typer.echo))


@app.command()
def backup(
    ctx: typer.Context,
    to: Annotated[
        Path | None,
        typer.Option(
            "--to",
            help="Write the copy into this folder instead, which must exist; nothing there is "
            "pruned.",
        ),
    ] = None,
) -> None:
    """Back the journal up now: a checked, self-contained copy as today's in runtime/backups,
    db_backups_keep kept, and copied to storage.backup_copy_dir if set. Exits 1 if the copy
    failed."""
    if to is not None and not to.is_dir():
        typer.echo(f"error: --to: {to} doesn't exist", err=True)
        raise typer.Exit(1)
    _run(_backup(ctx.obj, to))


async def _backup(root: Path, to: Path | None) -> None:
    from trader.ops.backup import backup_name, backups_dir, take_backup
    from trader.ops.disk import size_text
    from trader.runtime import open_local

    async with open_local(root) as local:
        storage = local.cfg.settings.storage
        today = local.clock.now().date()
        if to is not None:  # checked to exist above
            dest = to / backup_name(today)
            await local.journal.backup(dest)
            typer.echo(f"backup: {dest} ({size_text(dest.stat().st_size)})")
            return
        copy_dir = storage.backup_copy_dir
        taken = await take_backup(
            local.journal,
            folder=backups_dir(local.cfg.path(storage.db_path)),
            day=today,
            keep=storage.db_backups_keep,
            copy_dir=None if copy_dir is None else local.cfg.path(copy_dir),
        )
        typer.echo(f"backup: {taken.path} ({size_text(taken.size)})")
        for old in (*taken.pruned.removed, *taken.copy_pruned.removed):
            typer.echo(f"pruned: {old}")
        for problem in (*taken.pruned.problems, *taken.copy_pruned.problems):
            typer.echo(f"not pruned: {problem}")
        if taken.copy is not None:
            typer.echo(f"copied to {taken.copy}")
        if taken.copy_error is not None:
            typer.echo(f"not copied to storage.backup_copy_dir: {taken.copy_error}")
            raise typer.Exit(1)


@app.command()
def retention(
    ctx: typer.Context,
    apply: Annotated[
        bool, typer.Option("--apply", help="Delete them; without it, only list them.")
    ] = False,
) -> None:
    """List the logs, simulated and replayed days, recordings and LLM artifacts that retention
    deletes (storage.retention_days; recordings and LLM artifacts also stay through the current
    epoch). run-day applies it after each finished day. Exits 1 if any could not be deleted."""
    _run(_retention(ctx.obj, apply=apply))


async def _retention(root: Path, *, apply: bool) -> None:
    from trader.ops import retention as rules
    from trader.ops.disk import size_text
    from trader.runtime import open_local

    async with open_local(root) as local:
        plan = await rules.plan_retention(local.cfg, local.journal, local.clock.now().date())
        for line in rules.describe(plan, local.cfg.root):
            typer.echo(line)
        if not plan.expired:
            return
        if not apply:
            typer.echo("dry run: nothing deleted; `trader retention --apply` deletes them")
            return
        done = await asyncio.to_thread(rules.apply, plan)
        typer.echo(f"deleted {len(done.removed)} ({size_text(done.freed)})")
        failed = rules.failures(done, local.cfg.root)
        for line in failed:
            typer.echo(f"could not delete {line}")
        if failed:
            raise typer.Exit(1)


@app.command("weekly-job")
def weekly_job(ctx: typer.Context) -> None:
    """The Saturday job (A§1): `lessons evaluate --promote`, then `report weekly`. Each runs even
    if the other fails; exits 1 if either did."""
    failed: list[str] = []
    jobs: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = [
        ("lessons evaluate --promote", lambda: _lessons_evaluate(ctx.obj, promote=True)),
        ("report weekly", lambda: _report_weekly(ctx.obj, None, offline=False)),
    ]
    for name, job in jobs:
        typer.echo(f"-- {name}")
        try:
            _run(job())
        except typer.Exit as exc:
            if exc.exit_code:
                failed.append(name)
        except Exception as exc:  # one step's failure must not stop the other
            typer.echo(f"error: {type(exc).__name__}: {exc}", err=True)
            failed.append(name)
    if failed:
        typer.echo(f"weekly job: {', '.join(failed)} failed")
        raise typer.Exit(1)


def _logon(choice: str | None) -> Literal["s4u", "interactive"] | None:
    if choice is None:
        return None
    picked = choice.strip().lower()
    if picked == "s4u":
        return "s4u"
    if picked == "interactive":
        return "interactive"
    typer.echo(f"--logon takes s4u or interactive, not {choice!r}")
    raise typer.Exit(1)


def _config(root: Path) -> "AppConfig":
    from trader.config import ConfigError, load_config

    try:
        return load_config(root)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


def _schedule_deps() -> "ScheduleDeps":
    from trader.ops import schedule_cmds

    return schedule_cmds.default_deps()


# ---------------------------------------------------------------- smoke (HUMAN GATE G2)


@smoke_app.command("paper")
def smoke_paper(
    ctx: typer.Context,
    marketable: Annotated[
        bool, typer.Option("--marketable", help="Buy 1 share (paper), then flatten it.")
    ] = False,
    symbol: Annotated[str, typer.Option("--symbol", help="A liquid stock under $50.")] = "F",
    hold: Annotated[
        bool,
        typer.Option(
            "--hold",
            help="With --marketable: keep the share (G6b's fire drill). Needs a running engine.",
        ),
    ] = False,
) -> None:
    """HUMAN GATE G2: a 1-share OTO in Book A's paper account, through the risk gate. By
    default it rests and is cancelled; --marketable fills and flattens; --marketable --hold
    keeps the share beside a running engine (G6b). Refuses to compete with a running engine
    otherwise. Prints the journal."""
    if hold and not marketable:
        typer.echo("--hold needs --marketable: a resting order never fills")
        raise typer.Exit(1)
    _run(_smoke_paper(ctx.obj, symbol=symbol.strip().upper(), marketable=marketable, hold=hold))


async def _smoke_paper(root: Path, *, symbol: str, marketable: bool, hold: bool = False) -> None:
    from trader.core.calendar import day_times
    from trader.core.clock import ET
    from trader.core.events import EVENTS_FILE, load_events
    from trader.execution.alpaca_broker import AlpacaBroker
    from trader.execution.records import Records
    from trader.execution.service import ExecutionService, TradingDay
    from trader.execution.smoke import NoPrices, SmokeDeps, ensure_seeded, run_smoke
    from trader.memory.unattended import note_human
    from trader.orchestrator.heartbeat import heartbeat_path, read_heartbeat
    from trader.risk.breakers import Halts
    from trader.risk.gate import RiskGate
    from trader.risk.killswitch import KILL_FILE
    from trader.risk.ledger import SettlementLedger
    from trader.runtime import open_runtime

    async with open_runtime(root) as rt:
        cfg, clock = rt.cfg, rt.clock
        beat = read_heartbeat(heartbeat_path(cfg.path(cfg.settings.storage.db_path)), clock.now())
        if beat.fresh and not hold:  # its own service would compete with the engine's (D-083)
            typer.echo(
                "an engine is running (its heartbeat is fresh): smoke paper would compete with "
                "it for Book A. Stop it first, or use --marketable --hold (G6b)."
            )
            raise typer.Exit(1)
        if hold and not (beat.fresh and "A" in beat.books):
            typer.echo(
                "--hold leaves the share for the 15:55 flatten of a running engine that runs "
                "Book A: start run-day --books A,B first (G6b)"
            )
            raise typer.Exit(1)
        calendar = await rt.calendar()
        today = clock.now().date()
        session = calendar.session(today)  # not a trading day: a one-line error
        await note_human(rt.journal, clock, "trader smoke paper")  # it acts: not unattended
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
            if hold:
                await service.recover()  # the gate's equity; no loops: the engine owns the book
            else:
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
                ok = await run_smoke(
                    deps, symbol=symbol, marketable=marketable, echo=typer.echo, hold=hold
                )
            finally:
                await service.stop()
        finally:
            await broker.aclose()
    if not ok:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------- smoke (HUMAN GATE G4a)


@smoke_app.command("llm")
def smoke_llm(
    ctx: typer.Context,
    role: Annotated[
        str, typer.Option("--role", help="all, none, premarket, vet or review.")
    ] = "all",
    schema_mode: Annotated[
        str | None,
        typer.Option("--schema-mode", help="full or wire_safe. Default: llm.schema_mode."),
    ] = None,
    tools_flag: Annotated[
        str | None,
        typer.Option(
            "--tools-flag",
            help='empty (--tools "") or deny_all (--disallowedTools "*"). Default: llm.tools_flag.',
        ),
    ] = None,
    no_canary: Annotated[
        bool, typer.Option("--no-canary", help="Skip the isolation canary.")
    ] = False,
    canary_control: Annotated[
        bool,
        typer.Option(
            "--canary-control",
            help="Also run the canary with the isolation flags off, to show it sees a leak.",
        ),
    ] = False,
    kill_test: Annotated[
        bool,
        typer.Option("--kill-test", help="Also run a vet call with a 3 s timeout: the stop path."),
    ] = False,
) -> None:
    """HUMAN GATE G4a: real calls to the claude CLI (your subscription) on packs of fictional
    issuers, then an isolation canary. Nothing can trade; smoke calls never count toward the
    day's budget. Exits 1 unless the last line is SMOKE LLM PASSED."""
    _run(
        _smoke_llm(
            ctx.obj,
            role=role,
            schema_mode=schema_mode,
            tools_flag=tools_flag,
            canary=not no_canary,
            control=canary_control,
            kill_test=kill_test,
        )
    )


async def _smoke_llm(
    root: Path,
    *,
    role: str,
    schema_mode: str | None,
    tools_flag: str | None,
    canary: bool,
    control: bool,
    kill_test: bool,
) -> None:
    from trader.llm.smoke import run_smoke_llm, smoke_options
    from trader.orchestrator.lock import LOCK_NAME, RunLock
    from trader.runtime import open_local

    options = smoke_options(
        role=role,
        schema_mode=schema_mode,
        tools_flag=tools_flag,
        canary=canary,
        control=control,
        kill_test=kill_test,
    )
    async with open_local(root) as local:
        db = local.cfg.path(local.cfg.settings.storage.db_path)
        with RunLock(db.parent / LOCK_NAME):  # no session shares the sandbox meanwhile
            ok = await run_smoke_llm(local.cfg, local.journal, local.clock, typer.echo, options)
    if not ok:
        raise typer.Exit(code=1)


def _run(job: Coroutine[Any, Any, None]) -> None:
    """Run an async command; expected failures print one line and exit 1, never a traceback."""
    from trader.config import ConfigError
    from trader.core.calendar import CalendarRangeError, NotATradingDayError
    from trader.data.alpaca_rest import AlpacaError
    from trader.data.edgar import EdgarError
    from trader.execution.broker import BrokerError
    from trader.orchestrator.lock import RunLockError
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
        RunLockError,
        ValueError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
