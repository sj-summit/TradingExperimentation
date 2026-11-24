"""The weekly report (A§4.10; the Phase 5 plan's P5.4, decision 14; DECISIONS D-078).

One ISO week, read in one go (``build_weekly``), with the epoch's numbers to date beside it:
- **equity curves:** Book A, Book B and SPY, each indexed to 100 at the epoch's start;
- **each book's trades:** the week's and the epoch's (``memory/stats.py``);
- **Book B's vetoes:** V with its interval and each side's n, and the vetoed side's own mean
  shadow R, since V can be positive while the vetoes lose;
- **live R minus shadow R,** each book's mean (D-072's stop-fill bias, measured);
- **lessons** by status, and the promoted ones;
- **the factor tally** for the epoch, with S§6.5's caveat: factors are hindsight labels;
- **the week's ``bad_process`` reviews;**
- **open process issues and notes,** oldest and most recurring first (``followups``);
- **operations:** each of the week's sessions, unattended or why not, with its P1s, and the
  epoch's longest run of unattended sessions without a P1 (S§8 criterion 4, D-081);
- **the week's incidents;**
- **the epochs,** with Book B minus Book A in each;
- **the limitations footer.**

``report weekly`` fills in SPY's missing closes first (``fill_spy_closes``): a Friday's
``regime_daily`` row otherwise waits for Monday's boot. The report says when SPY's closes
don't reach the books' last session.
"""

import html
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import structlog

from trader.core.clock import Clock, parse_et_iso
from trader.llm.context import FactorCount
from trader.memory.epochs import EpochRow, current_epoch, epochs
from trader.memory.journal import Journal
from trader.memory.regime import RegimeMarket, ensure_regime
from trader.memory.reviews import Followup, factor_tally, followups
from trader.memory.stats import (
    Bootstrap,
    Difference,
    EpochSummary,
    Interval,
    b_minus_a,
    read_epoch,
    spy_path,
    summarize,
)
from trader.memory.unattended import (
    SessionRun,
    calendar_sessions,
    epoch_sessions,
    longest_clean_run,
    session_runs,
)
from trader.reports.render import LIMITATIONS, ModelText, md_row

log = structlog.get_logger(__name__)

HINDSIGHT = (
    "Factors are hindsight labels: after a loss it's easy to call a breakout false. They point "
    "at patterns worth testing; only a lesson over pre-decision fields can pass the gate "
    "(STRATEGY.md 6.5)."
)


@dataclass(frozen=True)
class EquityPoint:
    day: date
    a: float | None
    b: float | None
    spy: float | None


@dataclass(frozen=True)
class WeeklyReport:
    year: int
    week: int
    first: date  # the ISO week's Monday
    last: date  # its Friday
    epoch: int
    week_stats: EpochSummary
    epoch_stats: EpochSummary
    curve: tuple[EquityPoint, ...]  # the epoch to date
    lessons: Mapping[str, int]  # status -> count
    promoted: tuple[tuple[str, str, int | None, float | None], ...]  # id, statement, n, effect
    factors: tuple[FactorCount, ...]
    bad_process: tuple[tuple[str, str, str], ...]  # day, symbol, Book B's note (the model's)
    followups: tuple[Followup, ...]
    incidents: tuple[tuple[str, str, str, str, str], ...]  # time, severity, component, code, msg
    epochs: tuple[tuple[EpochRow, Difference], ...]
    spy_note: str | None  # when SPY's closes don't reach the books' last session
    sessions: tuple[SessionRun, ...] = ()  # the week's: unattended, or why not (D-081)
    longest_clean: int = 0  # the epoch's longest unattended run without a P1, to date


def iso_week(year: int, week: int) -> tuple[date, date]:
    monday = date.fromisocalendar(year, week, 1)
    return monday, monday + timedelta(days=4)


def last_complete_week(today: date) -> tuple[int, int]:
    """The ISO week whose Friday has passed: this week from Saturday, else the one before."""
    year, week, weekday = today.isocalendar()
    if weekday >= 6:
        return year, week
    previous = today - timedelta(days=7)
    return previous.isocalendar()[0], previous.isocalendar()[1]


def build_weekly(
    conn: sqlite3.Connection, *, year: int, week: int, boot: Bootstrap
) -> WeeklyReport:
    first, last = iso_week(year, week)
    epoch = current_epoch(conn)
    week_stats = summarize(read_epoch(conn, epoch, first=first, last=last), boot)
    to_date = read_epoch(conn, epoch, last=last)
    epoch_stats = summarize(to_date, boot)
    days = sorted({d.day for d in to_date.days})
    spy = spy_path(to_date.spy, days)
    bases = {
        book: next((d.start_equity for d in to_date.days if d.book == book), None)
        for book in ("A", "B")
    }
    ends = {(d.day, d.book): d.end_equity for d in to_date.days}

    def index(day: date, book: str) -> float | None:
        base, end = bases[book], ends.get((day, book))
        return None if base is None or end is None or base <= 0 else float(end / base * 100)

    curve = tuple(EquityPoint(d, index(d, "A"), index(d, "B"), spy.get(d)) for d in days)
    held = epoch_stats.spy
    spy_note = None
    if days and not held.covered:
        reach = held.end.isoformat() if held.end else "none of them"
        spy_note = f"SPY's closes reach {reach}, not the books' last session ({days[-1]})"
    span = (first.isoformat(), last.isoformat())
    return WeeklyReport(
        year=year,
        week=week,
        first=first,
        last=last,
        epoch=epoch,
        week_stats=week_stats,
        epoch_stats=epoch_stats,
        curve=curve,
        lessons=dict(
            conn.execute("SELECT status, COUNT(*) FROM lessons GROUP BY status ORDER BY status")
        ),
        promoted=tuple(
            (r["id"], r["statement"] or "", r["n_subset"], r["effect_r"])
            for r in conn.execute(
                "SELECT id, statement, n_subset, effect_r FROM lessons WHERE status = 'promoted' "
                "ORDER BY id"
            )
        ),
        factors=tuple(factor_tally(conn, epoch, last + timedelta(days=1))),
        bad_process=tuple(
            (r["date"], r["symbol"], r["b_note"] or "")
            for r in conn.execute(
                "SELECT r.date, s.symbol, r.b_note FROM signal_reviews r JOIN signals s "
                "ON s.id = r.signal_id WHERE r.b_process = 'bad_process' "
                "AND r.date BETWEEN ? AND ? ORDER BY r.date, s.symbol",
                span,
            )
        ),
        followups=tuple(followups(conn)),
        incidents=tuple(
            (
                parse_et_iso(r["ts"]).strftime("%Y-%m-%d %H:%M"),
                r["severity"] or "",
                r["component"] or "",
                r["code"] or "",
                r["message"] or "",
            )
            for r in conn.execute(
                "SELECT ts, severity, component, code, message FROM incidents "
                "WHERE substr(ts, 1, 10) BETWEEN ? AND ? ORDER BY ts, id",
                span,
            )
        ),
        epochs=tuple(
            (e, b_minus_a(read_epoch(conn, e.epoch, last=last).trades, boot)) for e in epochs(conn)
        ),
        spy_note=spy_note,
        sessions=tuple(session_runs(conn, calendar_sessions(conn, first, last))),
        longest_clean=longest_clean_run(session_runs(conn, epoch_sessions(conn, epoch, last))),
    )


async def fill_spy_closes(
    journal: Journal, market: RegimeMarket, clock: Clock, sessions: Sequence[date]
) -> list[date]:
    """Compute and store the sessions' missing ``regime_daily`` rows (``ensure_regime``, the boot
    job's own), and return the sessions still missing; a failure leaves them so."""
    missing = []
    for day in sessions:
        if await ensure_regime(journal=journal, market=market, clock=clock, d=day) is None:
            missing.append(day)
    return missing


# ---------------------------------------------------------------- rendering


def _f(value: float | None, fmt: str = "{:+.3f}") -> str:
    return "" if value is None else fmt.format(value)


def _ci(i: Interval | None) -> str:
    return "" if i is None else f"[{i.low:+.3f}, {i.high:+.3f}]"


def _tables(r: WeeklyReport) -> list[tuple[str, list[str], list[list[str]]]]:
    out: list[tuple[str, list[str], list[list[str]]]] = []
    out.append(
        (
            "Equity, the epoch to date (100 at its start)",
            ["session", "Book A", "Book B", "SPY"],
            [
                [p.day.isoformat(), _f(p.a, "{:.2f}"), _f(p.b, "{:.2f}"), _f(p.spy, "{:.2f}")]
                for p in r.curve
            ],
        )
    )
    for label, s in (("this week", r.week_stats), ("the epoch to date", r.epoch_stats)):
        out.append(
            (
                f"The books, {label}",
                [
                    "book",
                    "days",
                    "trades",
                    "win rate",
                    "mean R",
                    "net $/trade",
                    "profit factor",
                    "net P&L",
                    "max DD %",
                    "Sharpe",
                    "exposure",
                    "slippage",
                    "delay ms",
                    "live - shadow R",
                ],
                [
                    [
                        m.book,
                        str(m.days),
                        str(m.trades),
                        _f(m.win_rate, "{:.2f}"),
                        _f(m.mean_r),
                        _f(m.expectancy_net_usd, "{:+.2f}"),
                        _f(m.profit_factor, "{:.2f}"),
                        _f(m.net_pnl_usd, "{:+.2f}"),
                        _f(m.max_drawdown_pct, "{:.2f}"),
                        _f(m.daily_sharpe, "{:.2f}"),
                        _f(m.exposure, "{:.2f}"),
                        _f(m.mean_entry_slippage, "{:.3f}"),
                        _f(m.mean_entry_delay_ms, "{:.0f}"),
                        _f(m.live_minus_shadow_r)
                        + (f" (n {m.live_vs_shadow_n})" if m.live_vs_shadow_n else ""),
                    ]
                    for m in s.books.values()
                ],
            )
        )
    rows = []
    for label, s in (("this week", r.week_stats), ("the epoch to date", r.epoch_stats)):
        v = s.veto
        rows.append(
            [
                label,
                _f(v.v),
                _ci(v.v_interval),
                str(v.n_allowed),
                str(v.n_vetoed),
                _f(v.mean_vetoed),
                _ci(v.vetoed_interval),
                str(v.n_no_verdict),
                _f(s.b_minus_a.value),
                _ci(s.b_minus_a.interval),
            ]
        )
    out.append(
        (
            "Book B's vetoes: V = mean shadow R allowed - vetoed (90% interval, days resampled)",
            [
                "period",
                "V",
                "interval",
                "n allowed",
                "n vetoed",
                "vetoed's own mean R",
                "its interval",
                "no verdict",
                "B - A mean R",
                "its interval",
            ],
            rows,
        )
    )
    out.append(
        (
            "Lessons",
            ["status", "count"],
            [[status, str(n)] for status, n in sorted(r.lessons.items())],
        )
    )
    out.append(
        (
            "Promoted lessons",
            ["id", "statement", "n", "effect R"],
            [[i, ModelText(s), "" if n is None else str(n), _f(e)] for i, s, n, e in r.promoted],
        )
    )
    out.append(
        (
            "The factor tally, the epoch to date",
            ["factor", "targets", "wins", "losses", "mean shadow R", "knowable yes"],
            [
                [
                    f.factor,
                    str(f.targets),
                    str(f.wins),
                    str(f.losses),
                    _f(f.mean_shadow_r),
                    str(f.knowable_yes),
                ]
                for f in r.factors
            ],
        )
    )
    out.append(
        (
            "This week's bad_process reviews",
            ["session", "symbol", "Book B's note"],
            [[d, sym, ModelText(note)] for d, sym, note in r.bad_process],
        )
    )
    out.append(
        (
            "Open process issues and notes (trader followups list)",
            ["kind", "id", "severity", "first..last", "times", "text"],
            [
                [
                    f.kind,
                    str(f.id),
                    f.severity,
                    f"{f.first}..{f.last}",
                    str(f.count),
                    ModelText(f.text),
                ]
                for f in r.followups
            ],
        )
    )
    out.append(
        (
            "Operations: this week's sessions",
            ["session", "unattended", "why not", "P1s"],
            [
                [run.day.isoformat(), "yes" if run.unattended else "no", run.why_not, str(run.p1)]
                for run in r.sessions
            ],
        )
    )
    out.append(
        (
            "This week's incidents",
            ["time", "severity", "component", "code", "message"],
            [[t, sev, comp, code, ModelText(msg)] for t, sev, comp, code, msg in r.incidents],
        )
    )
    out.append(
        (
            "Epochs",
            ["epoch", "since", "DECISIONS", "reason", "B - A mean R", "interval"],
            [
                [
                    str(e.epoch),
                    (e.started_at or "the start")[:10],
                    e.decision or "",
                    e.reason,
                    _f(d.value),
                    _ci(d.interval),
                ]
                for e, d in r.epochs
            ],
        )
    )
    return out


def _title(r: WeeklyReport) -> str:
    return f"Weekly report {r.year}-W{r.week:02d} ({r.first} to {r.last})"


def _notes(r: WeeklyReport) -> list[str]:
    s = r.epoch_stats
    out = [f"epoch {r.epoch}; sessions in the epoch: {len(r.curve)}"]
    if s.spy.return_pct is not None:
        out.append(
            f"SPY bought at the {s.spy.start} close and held: {s.spy.return_pct:+.2f}% to "
            f"{s.spy.end}"
        )
    if r.spy_note:
        out.append(r.spy_note)
    out.append(
        f"the epoch's longest run of unattended sessions without a P1: {r.longest_clean} "
        "(STRATEGY.md 8, criterion 4, needs 20)"
    )
    out.append(HINDSIGHT)
    return out


def render_markdown(r: WeeklyReport) -> str:
    lines = [f"# {_title(r)}", ""] + [f"- {n}" for n in _notes(r)]
    for title, header, rows in _tables(r):
        lines += ["", f"## {title}", ""]
        if not rows:
            lines.append("(none)")
            continue
        lines += [md_row(header), "|" + "---|" * len(header)] + [md_row(row) for row in rows]
    lines += ["", "## Limitations", ""] + [f"- {item}" for item in LIMITATIONS] + [""]
    return "\n".join(lines)


def render_html(r: WeeklyReport) -> str:
    e = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{e(_title(r))}</title>",
        "<style>body{font-family:sans-serif;margin:1.5em}table{border-collapse:collapse;"
        "margin:.5em 0 1.5em}th,td{border:1px solid #ccc;padding:2px 8px;text-align:left}"
        "th{background:#f3f3f3}</style></head><body>",
        f"<h1>{e(_title(r))}</h1><ul>",
        *[f"<li>{e(n)}</li>" for n in _notes(r)],
        "</ul>",
        equity_svg(r.curve),
    ]
    for title, header, rows in _tables(r):
        parts.append(f"<h2>{e(title)}</h2>")
        if not rows:
            parts.append("<p>(none)</p>")
            continue
        parts.append("<table><tr>" + "".join(f"<th>{e(h)}</th>" for h in header) + "</tr>")
        parts += ["<tr>" + "".join(f"<td>{e(c)}</td>" for c in row) + "</tr>" for row in rows]
        parts.append("</table>")
    parts.append("<h2>Limitations</h2><ul>")
    parts += [f"<li>{e(item)}</li>" for item in LIMITATIONS]
    parts.append("</ul></body></html>")
    return "\n".join(parts)


SERIES = (("Book A", "a", "#1f77b4"), ("Book B", "b", "#ff7f0e"), ("SPY", "spy", "#7f7f7f"))


def equity_svg(curve: Sequence[EquityPoint], *, width: int = 640, height: int = 240) -> str:
    """The equity curves as an inline SVG line chart: no new dependency (decision 13)."""
    values = [v for p in curve for _, key, _ in SERIES if (v := getattr(p, key)) is not None]
    if not values:
        return "<p>(no equity to chart)</p>"
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    pad = 30
    step = (width - 2 * pad) / max(len(curve) - 1, 1)

    def y(v: float) -> float:
        return float(height - pad - (v - low) / span * (height - 2 * pad))

    out = [
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
        f"role='img' aria-label='equity curves'>",
        f"<text x='4' y='{pad}' font-size='11'>{high:.1f}</text>",
        f"<text x='4' y='{height - pad}' font-size='11'>{low:.1f}</text>",
    ]
    for k, (label, key, color) in enumerate(SERIES):
        points = [
            f"{pad + i * step:.1f},{y(v):.1f}"
            for i, p in enumerate(curve)
            if (v := getattr(p, key)) is not None
        ]
        if points:
            out.append(
                f"<polyline fill='none' stroke='{color}' stroke-width='2' "
                f"points='{' '.join(points)}'/>"
            )
        out.append(
            f"<text x='{pad + 90 * k}' y='14' font-size='12' fill='{color}'>"
            f"{html.escape(label)}</text>"
        )
    out.append("</svg>")
    return "\n".join(out)


def write_weekly(r: WeeklyReport, reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = f"weekly-{r.year}-{r.week:02d}"
    md, page = reports_dir / f"{stem}.md", reports_dir / f"{stem}.html"
    md.write_text(render_markdown(r), encoding="utf-8")
    page.write_text(render_html(r), encoding="utf-8")
    return md, page
