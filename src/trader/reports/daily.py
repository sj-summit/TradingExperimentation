"""The daily report (A§4.10; DECISIONS D-046, D-069), as Markdown and HTML from one data model.

Each book's columns show its decisions and results, "n/a" for a book that didn't run. When
Book B ran, the report adds its LLM work: the header's calls, budget, failed attempts and day
mode; Book B's verdict per signal; a table of the day's attempts; and Claude's review (P4.10
stores it). A day Book B was asked for but kept off says why.

HTML is built with ``html.escape`` (no template engine, no new dependency), so text from
incidents, symbols or the model can't inject markup. In Markdown, text the model wrote goes in a
code span, where nothing is markup: no link (bare URLs and e-mail addresses included, which GFM
and VS Code's preview would otherwise make clickable), image, HTML or emphasis.
"""

import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from trader.config import BookName
from trader.core.calendar import Session
from trader.core.clock import parse_et_iso
from trader.llm.budget import ABANDONED, CLI_RUNS
from trader.memory.journal import Journal, db_to_dec
from trader.strategy.orb_sip import load_signals

LIMITATIONS = (
    "Data: IEX-only real-time data makes triggers lag, and makes relative volume noisier.",
    "Paper fills: they ignore market impact, queue position, and latency slippage. Dividends "
    "aren't simulated either. Paper does charge regulatory fees, overnight; the ledger books "
    "what it charged.",
    "Sample size: small samples mean wide confidence intervals. Two to three months of signals "
    "cannot prove a durable edge.",
    "Backtest vs live: the paper's backtest (2016-2023, with leverage, long and short) is not a "
    "forecast. Day-trading edges decay, and most retail day traders lose money.",
    "LLM drift: model updates behind an alias can change behavior mid-epoch.",
)


@dataclass(frozen=True)
class TradeLine:
    symbol: str
    qty: int
    entry: Decimal
    exit: Decimal | None
    reason: str | None
    pnl: Decimal | None
    fees: Decimal
    r: float | None


@dataclass(frozen=True)
class SignalLine:
    rank: int
    symbol: str
    rel_vol: float
    direction: str
    status: str
    trigger: Decimal | None
    triggered_at: datetime | None
    shadow_r: float | None
    shadow_exit: str | None
    decisions: dict[str, str] = field(default_factory=dict)  # book -> final reason code
    results: dict[str, str] = field(default_factory=dict)  # book -> "R 1.23, $4.56"
    b_verdict: str = ""  # Book B's: "vet veto 0.80", the one its decision rests on if any


@dataclass(frozen=True)
class BookLine:
    book: BookName
    start_equity: Decimal | None
    end_equity: Decimal | None
    pnl: Decimal | None
    trades: tuple[TradeLine, ...]

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl is not None and t.pnl - t.fees > 0)


@dataclass(frozen=True)
class IncidentLine:
    ts: str
    severity: str
    component: str
    code: str
    message: str


@dataclass(frozen=True)
class CallLine:
    """One LLM attempt (a row of ``llm_calls``)."""

    attempt: str
    role: str
    started: str
    took_s: float | None
    result: str  # ok, or the failure
    model: str
    cost: float | None
    clamped: int | None


@dataclass(frozen=True)
class TargetLine:
    """One of the review's signal reviews: the model's text, escaped when rendered."""

    symbol: str
    went_well: str
    went_badly: str
    why: str
    b_process: str


@dataclass(frozen=True)
class MentionLine:
    """A process issue or note the day's review raised (A§4.10): new today or recurring."""

    kind: str  # issue | note
    item_id: int
    recurring: bool
    count: int  # how often it has been raised, today included
    severity: str
    text: str  # the model's, today's


@dataclass(frozen=True)
class LlmDay:
    """Book B's LLM work for the day."""

    calls: tuple[CallLine, ...] = ()
    cli_runs: int = 0  # toward max_calls_per_day
    failed: int = 0  # toward llm_failures_degrade_after
    abandoned: int = 0
    max_calls: int | None = None
    degrade_after: int | None = None
    day_mode: str | None = None  # "normal x1 (premarket, <call id>)"
    day_mode_reason: str = ""  # the model's
    review_status: str | None = None
    review_summary: str = ""  # the model's
    review_failure: str = ""  # why it is missing or invalid (the engine's words)
    reviews: tuple[TargetLine, ...] = ()
    raised: tuple[MentionLine, ...] = ()


@dataclass(frozen=True)
class DailyReport:
    day: date
    session: Session | None
    epoch: int
    mode: str
    books: tuple[BookName, ...]
    signals: tuple[SignalLine, ...]
    book_lines: tuple[BookLine, ...]
    incidents: tuple[IncidentLine, ...]
    halts: dict[str, list[str]]
    regime: dict[str, object]
    llm: LlmDay = LlmDay()
    book_b_off: str | None = None  # why Book B didn't run, though asked for

    def summary(self) -> str:
        """One line for the end-of-day notification (no secrets, no account numbers)."""
        books = "; ".join(
            f"{b.book}: {_signed(b.pnl)} ({len(b.trades)} trades, {b.wins} won)"
            for b in self.book_lines
        )
        armed = sum(1 for s in self.signals if s.status in ("armed", "triggered", "expired"))
        triggered = sum(1 for s in self.signals if s.triggered_at is not None)
        p1 = sum(1 for i in self.incidents if i.severity == "P1")
        p2 = sum(1 for i in self.incidents if i.severity == "P2")
        return (
            f"{self.day} | {books or 'no books'} | signals {len(self.signals)} "
            f"({armed} armed, {triggered} triggered) | incidents P1 {p1}, P2 {p2}"
        )


async def build_daily(
    journal: Journal,
    day: date,
    *,
    books: Sequence[BookName],
    mode: str,
    session: Session | None,
    now: datetime,
    book_b_off: str | None = None,
    max_calls: int | None = None,
    degrade_after: int | None = None,
) -> DailyReport:
    signals = await load_signals(journal, day)
    shadow = {
        r["signal_id"]: (r["shadow_r"], r["exit_reason"])
        for r in await journal.fetch_all(
            "SELECT signal_id, shadow_r, exit_reason FROM shadow_outcomes "
            "WHERE signal_id IN (SELECT id FROM signals WHERE date = ?)",
            (day.isoformat(),),
        )
    }
    finals: dict[str, dict[str, str]] = {}
    for r in await journal.fetch_all(
        "SELECT d.signal_id, d.book, d.reason_code FROM final_decisions d "
        "JOIN signals s ON s.id = d.signal_id WHERE s.date = ?",
        (day.isoformat(),),
    ):
        finals.setdefault(r["signal_id"], {})[r["book"]] = r["reason_code"]
    trades: dict[str, dict[str, TradeLine]] = {}
    book_lines = []
    for book in books:
        rows = await journal.fetch_all(
            "SELECT * FROM trades WHERE book = ? AND entry_ts LIKE ? ORDER BY entry_ts",
            (book, f"{day.isoformat()}T%"),
        )
        lines = []
        for r in rows:
            line = TradeLine(
                symbol=r["symbol"],
                qty=int(r["qty"]),
                entry=Decimal(r["entry_price"]),
                exit=db_to_dec(r["exit_price"]),
                reason=r["exit_reason"],
                pnl=db_to_dec(r["pnl"]),
                fees=db_to_dec(r["fees_est"]) or Decimal(0),
                r=r["r_multiple"],
            )
            lines.append(line)
            trades.setdefault(r["signal_id"], {})[book] = line
        stats = await journal.fetch_one(
            "SELECT start_equity, end_equity, pnl FROM daily_stats WHERE date = ? AND book = ?",
            (day.isoformat(), book),
        )
        book_lines.append(
            BookLine(
                book=book,
                start_equity=db_to_dec(stats["start_equity"]) if stats else None,
                end_equity=db_to_dec(stats["end_equity"]) if stats else None,
                pnl=(
                    db_to_dec(stats["pnl"])
                    if stats
                    else sum((t.pnl - t.fees for t in lines if t.pnl is not None), Decimal(0))
                ),
                trades=tuple(lines),
            )
        )
    b_verdicts = await _b_verdicts(journal, day) if "B" in books else {}
    signal_lines = tuple(
        SignalLine(
            rank=s.rel_vol_rank,
            symbol=s.symbol,
            rel_vol=s.rel_vol,
            direction=s.direction,
            status=s.status,
            trigger=s.trigger_price,
            triggered_at=s.triggered_at,
            shadow_r=shadow.get(s.id, (None, None))[0],
            shadow_exit=shadow.get(s.id, (None, None))[1],
            decisions=finals.get(s.id, {}),
            results={b: _result(t) for b, t in trades.get(s.id, {}).items()},
            b_verdict=b_verdicts.get(s.symbol, ""),
        )
        for s in signals
    )
    incidents = tuple(
        IncidentLine(r["ts"], r["severity"], r["component"], r["code"], r["message"])
        for r in await journal.fetch_all(
            "SELECT ts, severity, component, code, message FROM incidents WHERE ts LIKE ? "
            "ORDER BY id",
            (f"{day.isoformat()}T%",),
        )
    )
    halts: dict[str, list[str]] = {}
    for r in await journal.fetch_all(
        "SELECT book, reason FROM halts WHERE cleared_at IS NULL "
        "AND (expires_at IS NULL OR expires_at > ?) ORDER BY id",
        (now.isoformat(),),
    ):
        halts.setdefault(r["book"], []).append(r["reason"])
    epoch = int(await journal.get_meta("eval_epoch") or 0)
    regime_row = await journal.fetch_one(
        "SELECT regime_json FROM daily_stats WHERE date = ? AND regime_json IS NOT NULL",
        (day.isoformat(),),
    )
    regime = json.loads(regime_row["regime_json"]) if regime_row else {}
    if not regime and signals:
        regime = {k: v for k, v in signals[0].features.items()
                  if k in ("regime_spy_trend", "regime_vol_bucket", "event_day")}  # fmt: skip
    llm = (
        await _llm_day(journal, day, max_calls=max_calls, degrade_after=degrade_after)
        if "B" in books
        else LlmDay()
    )
    if book_b_off is None and "B" not in books:  # as the day noted it (a regenerated report)
        note = await journal.fetch_one(
            "SELECT detail FROM day_runs WHERE date = ? AND kind = 'note' AND name = 'book_b_off' "
            "ORDER BY id DESC LIMIT 1",
            (day.isoformat(),),
        )
        book_b_off = note["detail"] if note is not None else None
    return DailyReport(day=day, session=session, epoch=epoch, mode=mode, books=tuple(books),
                       signals=signal_lines, book_lines=tuple(book_lines), incidents=incidents,
                       halts=halts, regime=regime, llm=llm, book_b_off=book_b_off)  # fmt: skip


async def _b_verdicts(journal: Journal, day: date) -> dict[str, str]:
    """Book B's verdict per symbol: the one its decision rests on, else the day's latest."""
    d = day.isoformat()
    rows = await journal.fetch_all(
        "SELECT symbol, source, verdict, confidence, llm_call_id FROM verdicts v WHERE date = ? "
        "AND llm_call_id IN (SELECT id FROM llm_calls WHERE purpose = 'trading') ORDER BY id",
        (d,),
    )
    rested = {
        (r["symbol"], r["llm_call_id"])
        for r in await journal.fetch_all(
            "SELECT s.symbol, f.llm_call_id FROM final_decisions f JOIN signals s "
            "ON s.id = f.signal_id WHERE s.date = ? AND f.book = 'B' AND f.llm_call_id IS NOT NULL",
            (d,),
        )
    }
    out: dict[str, str] = {}
    chosen: set[str] = set()
    for r in rows:  # oldest first: the latest wins, unless the decision named one
        text = f"{r['source']} {r['verdict']} {r['confidence']:.2f}"
        if (r["symbol"], r["llm_call_id"]) in rested:
            out[r["symbol"]] = text
            chosen.add(r["symbol"])
        elif r["symbol"] not in chosen:
            out[r["symbol"]] = text
    return out


async def _llm_day(
    journal: Journal, day: date, *, max_calls: int | None, degrade_after: int | None
) -> LlmDay:
    d = day.isoformat()
    rows = await journal.fetch_all(
        "SELECT id, role, started_at, duration_ms, ok, failure, model_resolved, model_alias, "
        "cost_estimate_usd, clamped FROM llm_calls WHERE date = ? AND purpose = 'trading' "
        "ORDER BY started_at, id",
        (d,),
    )
    calls = tuple(
        CallLine(
            attempt=r["id"],
            role=r["role"],
            started=_clock(r["started_at"] or ""),
            took_s=None if r["duration_ms"] is None else r["duration_ms"] / 1000,
            result="ok" if r["ok"] else (r["failure"] or "open"),
            model=r["model_resolved"] or r["model_alias"] or "",
            cost=r["cost_estimate_usd"],
            clamped=r["clamped"],
        )
        for r in rows
    )
    runs = await journal.fetch_one(
        f"SELECT COUNT(*) AS n FROM llm_calls WHERE date = ? AND purpose = 'trading' "
        f"AND {CLI_RUNS}",
        (d,),
    )
    mode = await journal.fetch_one(
        "SELECT mode, source, multiplier, reason, llm_call_id FROM day_modes "
        "WHERE date = ? AND book = 'B'",
        (d,),
    )
    review = await journal.fetch_one(
        "SELECT status, summary, failure FROM daily_reviews WHERE date = ?", (d,)
    )
    raised = await journal.fetch_all(
        "SELECT m.kind, m.item_id, m.severity, m.text, "
        "COALESCE(i.first_seen, n.first_raised) AS first, "
        "COALESCE(i.occurrences, n.times_raised) AS count FROM review_mentions m "
        "LEFT JOIN process_issues i ON m.kind = 'issue' AND i.id = m.item_id "
        "LEFT JOIN review_notes n ON m.kind = 'note' AND n.id = m.item_id "
        "WHERE m.date = ? ORDER BY m.kind, m.id",
        (d,),
    )
    targets = await journal.fetch_all(
        "SELECT s.symbol, r.went_well, r.went_badly, r.why, r.b_process FROM signal_reviews r "
        "JOIN signals s ON s.id = r.signal_id WHERE r.date = ? ORDER BY s.rel_vol_rank",
        (d,),
    )
    day_mode = None
    if mode is not None:
        source = mode["source"] or "premarket"
        call = f", {mode['llm_call_id']}" if mode["llm_call_id"] else ""
        day_mode = f"{mode['mode']} x{mode['multiplier'] or '?'} ({source}{call})"
    return LlmDay(
        calls=calls,
        cli_runs=int(runs["n"]) if runs else 0,
        failed=sum(1 for c in calls if c.result not in ("ok", ABANDONED, "open")),
        abandoned=sum(1 for c in calls if c.result == ABANDONED),
        max_calls=max_calls,
        degrade_after=degrade_after,
        day_mode=day_mode,
        day_mode_reason=(mode["reason"] or "") if mode is not None else "",
        review_status=review["status"] if review is not None else None,
        review_summary=(review["summary"] or "") if review is not None else "",
        review_failure=(review["failure"] or "") if review is not None else "",
        raised=tuple(
            MentionLine(
                kind=r["kind"],
                item_id=int(r["item_id"]),
                recurring=(r["first"] or d) < d,
                count=int(r["count"] or 1),
                severity=r["severity"] or "",
                text=r["text"] or "",
            )
            for r in raised
        ),
        reviews=tuple(
            TargetLine(
                symbol=t["symbol"],
                went_well=t["went_well"] or "",
                went_badly=t["went_badly"] or "",
                why=t["why"] or "",
                b_process=t["b_process"] or "",
            )
            for t in targets
        ),
    )


# ---------------------------------------------------------------- rendering


class ModelText(str):
    """Text the model wrote: Markdown escapes all of it (HTML escapes every cell anyway)."""


def _tables(report: DailyReport) -> list[tuple[str, list[str], list[list[str]]]]:
    """(title, header, rows) for every table; shared by Markdown and HTML."""

    def book_cell(s: SignalLine, b: BookName) -> str:
        if b not in report.books:
            return "n/a"
        return f"{s.decisions.get(b, '')} {s.results.get(b, '')}".strip()

    signal_rows = [
        [
            str(s.rank),
            s.symbol,
            f"{s.rel_vol:.2f}",
            s.direction,
            s.status,
            _txt(s.trigger),
            s.triggered_at.strftime("%H:%M:%S") if s.triggered_at else "",
            _num(s.shadow_r),
            s.shadow_exit or "",
            book_cell(s, "A"),
            s.b_verdict if "B" in report.books else "n/a",
            book_cell(s, "B"),
        ]
        for s in report.signals
    ]
    tables = [
        (
            "Signals (top 20)",
            ["#", "symbol", "rel_vol", "direction", "status", "trigger", "triggered",
             "shadow R", "shadow exit", "Book A", "B verdict", "Book B"],
            signal_rows,
        ),
        (
            "Books",
            ["book", "start equity", "end equity", "P&L (net)", "trades", "won"],
            [[b.book, _txt(b.start_equity), _txt(b.end_equity), _signed(b.pnl),
              str(len(b.trades)), str(b.wins)] for b in report.book_lines],
        ),
    ]  # fmt: skip
    for b in report.book_lines:
        tables.append(
            (
                f"Book {b.book} trades",
                ["symbol", "qty", "entry", "exit", "exit reason", "P&L (gross)", "fees", "R"],
                [[t.symbol, str(t.qty), _px(t.entry), _px(t.exit), t.reason or "open",
                  _signed(t.pnl), _txt(t.fees), _num(t.r)] for t in b.trades],
            )
        )  # fmt: skip
    tables.append(
        (
            "Incidents",
            ["time", "severity", "component", "code", "message"],
            [[_clock(i.ts), i.severity, i.component, i.code, i.message] for i in report.incidents],
        )
    )
    llm = report.llm
    if llm.calls:
        tables.append(
            (
                "LLM calls",
                ["attempt", "role", "started", "took (s)", "result", "model", "cost (est.)",
                 "clamped"],
                [[c.attempt, c.role, c.started, _num(c.took_s), c.result, c.model,
                  "" if c.cost is None else f"${c.cost:.2f}", _txt(c.clamped)]
                 for c in llm.calls],
            )
        )  # fmt: skip
    if llm.reviews:
        tables.append(
            (
                "Claude's review: the targets",
                ["symbol", "went well", "went badly", "why", "Book B's process"],
                [[t.symbol, ModelText(t.went_well), ModelText(t.went_badly), ModelText(t.why),
                  t.b_process] for t in llm.reviews],
            )
        )  # fmt: skip
    if llm.raised:
        tables.append(
            (
                "Claude's review: process issues and notes (`trader followups list`)",
                ["kind", "id", "new or recurring", "severity", "today's text"],
                [[m.kind, str(m.item_id), f"recurring (x{m.count})" if m.recurring else "new",
                  m.severity, ModelText(m.text)] for m in llm.raised],
            )
        )  # fmt: skip
    return tables


def _header_lines(report: DailyReport) -> list[str]:
    s = report.session
    hours = (
        f"session {s.open:%H:%M}-{s.close:%H:%M} ET{' (early close)' if s.is_early_close else ''}"
        if s
        else "no session"
    )
    regime = ", ".join(f"{k} {v}" for k, v in sorted(report.regime.items())) or "unknown"
    halts = "; ".join(f"{b}: {', '.join(r)}" for b, r in sorted(report.halts.items())) or "none"
    return [
        f"{hours} | epoch {report.epoch} | mode {report.mode} | books {', '.join(report.books)}",
        f"regime in force (prior session): {regime}",
        f"halts in force: {halts}",
        *_llm_lines(report),
    ]


def _llm_lines(report: DailyReport) -> list[str]:
    """The header's LLM lines: Book B's calls, budget, failed attempts and day mode."""
    if "B" not in report.books:
        why = f"Book B off: {report.book_b_off}" if report.book_b_off else "Book B not run"
        return [f"LLM calls: none ({why})"]
    llm = report.llm
    ok = sum(1 for c in llm.calls if c.result == "ok")
    counted = f"{ok} ok, {llm.failed} failed" + (
        f", {llm.abandoned} abandoned" if llm.abandoned else ""
    )
    budget = f"/{llm.max_calls}" if llm.max_calls is not None else ""
    toward = f"/{llm.degrade_after}" if llm.degrade_after is not None else ""
    return [
        f"LLM calls: {len(llm.calls)} attempts ({counted}); budget {llm.cli_runs}{budget} CLI "
        f"runs; failed attempts {llm.failed}{toward} toward llm_degraded",
        f"Book B day mode: {llm.day_mode or 'not set'}",
    ]


def _claude_md(report: DailyReport) -> list[str]:
    """Claude's own words: the day mode's reason and the review's summary, escaped."""
    llm = report.llm
    if "B" not in report.books or not (llm.day_mode_reason or llm.review_status):
        return []
    out = ["", "## Claude", ""]
    if llm.day_mode_reason:
        out.append(f"- The day mode's reason: {_model_md(llm.day_mode_reason)}")
    if llm.review_status == "ok":
        out.append(f"- The review (ok): {_model_md(llm.review_summary)}")
    elif llm.review_status:  # the engine's words, not the model's
        out.append(f"- The review ({llm.review_status}): {llm.review_failure or 'no reason given'}")
    return out


def _claude_html(report: DailyReport) -> list[str]:
    llm, e = report.llm, html.escape
    if "B" not in report.books or not (llm.day_mode_reason or llm.review_status):
        return []
    out = ["<h2>Claude</h2><ul>"]
    if llm.day_mode_reason:
        out.append(f"<li>The day mode's reason: {e(llm.day_mode_reason)}</li>")
    if llm.review_status:
        said = llm.review_summary if llm.review_status == "ok" else llm.review_failure
        out.append(f"<li>The review ({e(llm.review_status)}): {e(said or 'no reason given')}</li>")
    out.append("</ul>")
    return out


def render_markdown(report: DailyReport) -> str:
    lines = [f"# Daily report {report.day}", ""]
    lines += [f"- {line}" for line in _header_lines(report)]
    lines += _claude_md(report)
    for title, header, rows in _tables(report):
        lines += ["", f"## {title}", ""]
        if not rows:
            lines.append("(none)")
            continue
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
        lines += [
            "| "
            + " | ".join(
                _model_md(c, in_table=True) if isinstance(c, ModelText) else _md(c) for c in row
            )
            + " |"
            for row in rows
        ]
    lines += ["", "## Limitations", ""] + [f"- {item}" for item in LIMITATIONS] + [""]
    return "\n".join(lines)


def render_html(report: DailyReport) -> str:
    e = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>Daily report {e(str(report.day))}</title>",
        "<style>body{font-family:sans-serif;margin:1.5em}table{border-collapse:collapse;"
        "margin:.5em 0 1.5em}th,td{border:1px solid #ccc;padding:2px 8px;text-align:left}"
        "th{background:#f3f3f3}</style></head><body>",
        f"<h1>Daily report {e(str(report.day))}</h1><ul>",
        *[f"<li>{e(line)}</li>" for line in _header_lines(report)],
        "</ul>",
        *_claude_html(report),
    ]
    for title, header, rows in _tables(report):
        parts.append(f"<h2>{e(title)}</h2>")
        if not rows:
            parts.append("<p>(none)</p>")
            continue
        parts.append("<table><tr>" + "".join(f"<th>{e(h)}</th>" for h in header) + "</tr>")
        parts += ["<tr>" + "".join(f"<td>{e(cell)}</td>" for cell in row) + "</tr>" for row in rows]
        parts.append("</table>")
    parts.append("<h2>Limitations</h2><ul>")
    parts += [f"<li>{e(item)}</li>" for item in LIMITATIONS]
    parts.append("</ul></body></html>")
    return "\n".join(parts)


def write_daily(report: DailyReport, reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    md = reports_dir / f"{report.day.isoformat()}.md"
    page = reports_dir / f"{report.day.isoformat()}.html"
    md.write_text(render_markdown(report), encoding="utf-8")
    page.write_text(render_html(report), encoding="utf-8")
    return md, page


def _result(t: TradeLine) -> str:
    if t.exit is None:
        return "(open)"
    return f"R {_num(t.r)}, {_signed(t.pnl - t.fees if t.pnl is not None else None)}"


def _clock(ts: str) -> str:
    try:
        return parse_et_iso(ts).strftime("%H:%M:%S")
    except ValueError:
        return ts


def _md(cell: str) -> str:
    return cell.replace("|", "\\|").replace("\n", " ")


def _model_md(text: str, *, in_table: bool = False) -> str:
    """The model's text for Markdown: on one line, in a code span longer than any run of
    backticks it holds (the module's docstring). In a table cell its pipes are escaped too,
    which GFM requires even inside a code span."""
    flat = " ".join(text.split())
    if not flat:
        return ""
    fence = "`" * (max((len(run) for run in re.findall(r"`+", flat)), default=0) + 1)
    pad = " " if flat.startswith("`") or flat.endswith("`") else ""
    if in_table:
        flat = flat.replace("|", "\\|")
    return f"{fence}{pad}{flat}{pad}{fence}"


def _txt(value: object) -> str:
    return "" if value is None else str(value)


def _px(value: Decimal | None) -> str:
    return "" if value is None else f"{value:.4f}".rstrip("0").rstrip(".")


def _num(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def _signed(value: Decimal | None) -> str:
    if value is None:
        return ""
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"
