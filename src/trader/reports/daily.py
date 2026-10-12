"""The daily report (A§4.10; DECISIONS D-046), as Markdown and HTML from one data model.

Phase 3 fills Book A's columns; Book B's show "n/a" until Phase 4, and the LLM section says
none. HTML is built with ``html.escape`` (no template engine, no new dependency), so text from
incidents or symbols can't inject markup.
"""

import html
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from trader.config import BookName
from trader.core.calendar import Session
from trader.core.clock import parse_et_iso
from trader.memory.journal import Journal, db_to_dec
from trader.strategy.orb_sip import load_signals

LIMITATIONS = (
    "Data: IEX-only real-time data makes triggers lag, and makes relative volume noisier.",
    "Paper fills: they ignore market impact, queue position, and latency slippage. Regulatory "
    "fees and dividends aren't simulated either.",
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
    return DailyReport(day=day, session=session, epoch=epoch, mode=mode, books=tuple(books),
                       signals=signal_lines, book_lines=tuple(book_lines), incidents=incidents,
                       halts=halts, regime=regime)  # fmt: skip


# ---------------------------------------------------------------- rendering


def _tables(report: DailyReport) -> list[tuple[str, list[str], list[list[str]]]]:
    """(title, header, rows) for every table; shared by Markdown and HTML."""
    booked = ("A", "B")
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
            *[
                (
                    f"{s.decisions.get(b, '')} {s.results.get(b, '')}".strip()
                    if b in report.books
                    else "n/a"
                )
                for b in booked
            ],
        ]
        for s in report.signals
    ]
    tables = [
        (
            "Signals (top 20)",
            ["#", "symbol", "rel_vol", "direction", "status", "trigger", "triggered",
             "shadow R", "shadow exit", "Book A", "Book B"],
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
        "LLM calls: none (Book B and Claude arrive in Phase 4)",
    ]


def render_markdown(report: DailyReport) -> str:
    lines = [f"# Daily report {report.day}", ""]
    lines += [f"- {line}" for line in _header_lines(report)]
    for title, header, rows in _tables(report):
        lines += ["", f"## {title}", ""]
        if not rows:
            lines.append("(none)")
            continue
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
        lines += ["| " + " | ".join(_md(cell) for cell in row) + " |" for row in rows]
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
