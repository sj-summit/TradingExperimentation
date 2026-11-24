"""The evaluation memo (STRATEGY.md §8; the Phase 5 plan's P5.5, decision 15; DECISIONS D-079).

One epoch, judged by the rules pre-registered in S§8. Each line is ``pass``, ``fail`` or
``insufficient data``, with its numbers; nothing here decides to trade live. Epoch 0 is the
shakedown (S§9), so its memo says it is a dry run.
- **The hypotheses** (intervals at 90%, whole days resampled; ``memory/stats.py``):
  - H1: V's interval lies above 0.
  - H2: Book B's mean R per trade minus Book A's is at least 0 (its interval is shown).
  - H3: on Book B's ``reduced`` and ``no_trade`` days, the triggered longs' mean shadow R minus
    that on ``normal`` days has an interval below 0. It needs ``min_n_per_side`` signals on
    each side.
  - H4: ranks 1-5 minus ranks 16-20 has an interval above 0, beside a Spearman correlation of
    rank against shadow R (H4 predicts it negative). It needs ``min_n_per_side`` signals in
    each bucket.
- **The minimum data:** 60 trading days, 150 triggered longs, and 20 vetoes, meaning triggered
  longs with a standing veto, the population H1 is tested on. A triggered long is one whose
  shadow triggered, on SIP bars alone included (D-075). The research-only shadow shorts never
  count: no book can trade them.
- H1, H2 and criteria 1-3 wait for the minimum data. H3 and H4 have their own n per side.
- **The go-live criteria:**
  1. V's interval's lower bound is above 0.
  2. Book B's mean R per trade after its estimated fees is above 0.
  3. Book B's max drawdown is at most 15%.
  4. 20 consecutive unattended sessions without a P1 (``memory/unattended.py``, D-081): some
     run of 20 of the epoch's calendar sessions, each unattended and P1-free, passes;
     otherwise insufficient data while the epoch has fewer than 60 sessions, then fail. A
     session with no run breaks the streak.
  5. The human signs off: always "for the human".
- **The outcome** (S§8's table, with the inconclusive row the human approved on 2026-11-21):
  - **inconclusive** when, by 90 trading days, the minimum data isn't reached, or H1 has fewer
    than 20 vetoes, or V's interval includes 0 while wider than 0.5 R;
  - else **keep running** while the minimum data isn't reached, or V's interval spans 0 by more
    than 0.5 R (the 90th day would call that inconclusive, so an earlier look doesn't call it
    a fail);
  - else **both books negative** or **H1 fails, Book A positive**, by each book's net P&L;
  - else **the criteria code can check pass** when criteria 1-4 do, and the decision is the
    human's (G7);
  - else **criteria not met**.
"""

import html
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

import numpy as np

from trader.config import LessonsConfig
from trader.memory.epochs import current_epoch
from trader.memory.lessons import FeatureRow, read_features
from trader.memory.stats import (
    Bootstrap,
    EpochSummary,
    Interval,
    difference_interval,
    read_epoch,
    summarize,
)
from trader.memory.unattended import epoch_sessions, longest_clean_run, session_runs
from trader.reports.render import LIMITATIONS, md_row

Verdict = Literal["pass", "fail", "insufficient data", "for the human"]

MIN_DAYS = 60
MIN_TRIGGERED = 150
MIN_VETOES = 20
MAX_DRAWDOWN_PCT = 15.0
UNATTENDED_SESSIONS = 20
INCONCLUSIVE_DAYS = 90
INCONCLUSIVE_WIDTH_R = 0.5
TOP, BOTTOM = range(1, 6), range(16, 21)


@dataclass(frozen=True)
class Line:
    name: str
    statement: str
    verdict: Verdict
    numbers: str


@dataclass(frozen=True)
class Memo:
    epoch: int
    through: date
    days: int
    triggered: int
    vetoes: int
    minimum_met: bool
    hypotheses: tuple[Line, ...]
    minimum: tuple[Line, ...]
    criteria: tuple[Line, ...]
    outcome: str
    next_step: str
    summary: EpochSummary


def _ci(i: Interval | None) -> str:
    return "no interval" if i is None else f"90% interval [{i.low:+.3f}, {i.high:+.3f}]"


def _r(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.3f} R"


def _n(k: int, one: str, many: str) -> str:
    return f"{k} {one if k == 1 else many}"


def counts(m: Memo) -> str:
    """The memo's data in a phrase: "62 trading days, 248 triggered longs, 62 Book B vetoes"."""
    return ", ".join(
        (
            _n(m.days, "trading day", "trading days"),
            _n(m.triggered, "triggered long", "triggered longs"),
            _n(m.vetoes, "Book B veto", "Book B vetoes"),
        )
    )


def _diff(
    rows: Sequence[FeatureRow], first: set[str], second: set[str], boot: Bootstrap
) -> tuple[float | None, Interval | None, int, int]:
    by_day: dict[date, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
    for r in rows:
        if r.signal_id in first:
            by_day[r.day][0].append(r.shadow_r)
        elif r.signal_id in second:
            by_day[r.day][1].append(r.shadow_r)
    days = [by_day[d] for d in sorted(by_day)]
    a = [x for s, _ in days for x in s]
    b = [x for _, s in days for x in s]
    value = sum(a) / len(a) - sum(b) / len(b) if a and b else None
    return value, difference_interval(days, boot) if a and b else None, len(a), len(b)


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Rank correlation, ties averaged; None under three pairs or with no variation."""
    if len(x) < 3:
        return None

    def ranks(v: Sequence[float]) -> np.ndarray:
        arr = np.asarray(v, dtype=float)
        order = arr.argsort(kind="stable")
        out = np.empty(len(arr))
        out[order] = np.arange(len(arr), dtype=float)
        for value in np.unique(arr):  # ties share their mean rank
            same = arr == value
            out[same] = out[same].mean()
        return out

    rx, ry = ranks(x), ranks(y)
    if rx.std() == 0 or ry.std() == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def build_memo(
    conn: sqlite3.Connection, *, through: date, cfg: LessonsConfig, epoch: int | None = None
) -> Memo:
    boot = Bootstrap.of(cfg)
    epoch = current_epoch(conn) if epoch is None else epoch
    summary = summarize(read_epoch(conn, epoch, last=through), boot)
    rows = read_features(conn, epoch, last=through)
    days = len(
        conn.execute(
            "SELECT DISTINCT date FROM daily_stats WHERE eval_epoch = ? AND date <= ?",
            (epoch, through.isoformat()),
        ).fetchall()
    )
    veto = summary.veto
    triggered, vetoes = len(rows), veto.n_vetoed
    minimum = (
        Line("days", f"at least {MIN_DAYS} trading days", _at_least(days, MIN_DAYS), str(days)),
        Line(
            "triggered",
            f"at least {MIN_TRIGGERED} triggered longs",
            _at_least(triggered, MIN_TRIGGERED),
            str(triggered),
        ),
        Line(
            "vetoes",
            f"at least {MIN_VETOES} Book B vetoes on triggered longs",
            _at_least(vetoes, MIN_VETOES),
            str(vetoes),
        ),
    )
    minimum_met = all(m.verdict == "pass" for m in minimum)
    hypotheses = (
        _h1(summary, minimum_met),
        _h2(summary, minimum_met),
        _h3(conn, epoch, rows, cfg, boot),
        _h4(rows, cfg, boot),
    )
    criteria = _criteria(conn, epoch, summary, through, minimum_met)
    outcome, step = _outcome(days, minimum_met, summary, hypotheses, criteria)
    return Memo(
        epoch,
        through,
        days,
        triggered,
        vetoes,
        minimum_met,
        hypotheses,
        minimum,
        criteria,
        outcome,
        step,
        summary,
    )


def _at_least(n: int, need: int) -> Verdict:
    return "pass" if n >= need else "insufficient data"


def _h1(s: EpochSummary, enough: bool) -> Line:
    v = s.veto
    numbers = (
        f"V {_r(v.v)}, {_ci(v.v_interval)}; allowed n {v.n_allowed}, vetoed n {v.n_vetoed}, "
        f"the vetoed's own mean {_r(v.mean_vetoed)}"
    )
    if not enough or v.v_interval is None:
        verdict: Verdict = "insufficient data"
    else:
        verdict = "pass" if v.v_interval.low > 0 else "fail"
    return Line("H1", "Claude's vetoes have information value: V > 0", verdict, numbers)


def _h2(s: EpochSummary, enough: bool) -> Line:
    d = s.b_minus_a
    numbers = f"B - A {_r(d.value)} per trade, {_ci(d.interval)}; B n {d.n_first}, A n {d.n_second}"
    verdict: Verdict = (
        "insufficient data" if not enough or d.value is None else "pass" if d.value >= 0 else "fail"
    )
    return Line("H2", "Book B's mean R per trade >= Book A's", verdict, numbers)


def _h3(
    conn: sqlite3.Connection,
    epoch: int,
    rows: Sequence[FeatureRow],
    cfg: LessonsConfig,
    boot: Bootstrap,
) -> Line:
    modes = dict(
        conn.execute(
            "SELECT date, mode FROM day_modes WHERE book = 'B' AND eval_epoch = ?", (epoch,)
        ).fetchall()
    )
    low = {r.signal_id for r in rows if modes.get(r.day.isoformat()) in ("reduced", "no_trade")}
    normal = {r.signal_id for r in rows if modes.get(r.day.isoformat()) == "normal"}
    value, interval, n_low, n_normal = _diff(rows, low, normal, boot)
    numbers = f"reduced/no_trade - normal {_r(value)}, {_ci(interval)}; n {n_low} and {n_normal}"
    need = cfg.min_n_per_side
    if n_low < need or n_normal < need or interval is None:
        verdict: Verdict = "insufficient data"
    else:
        verdict = "pass" if interval.high < 0 else "fail"
    return Line("H3", "On reduced or no_trade days, triggered longs do worse", verdict, numbers)


def _h4(rows: Sequence[FeatureRow], cfg: LessonsConfig, boot: Bootstrap) -> Line:
    ranked = [r for r in rows if r.values.get("rel_vol_rank") is not None]
    top = {r.signal_id for r in ranked if int(r.values["rel_vol_rank"]) in TOP}
    bottom = {r.signal_id for r in ranked if int(r.values["rel_vol_rank"]) in BOTTOM}
    value, interval, n_top, n_bottom = _diff(rows, top, bottom, boot)
    rho = spearman([float(r.values["rel_vol_rank"]) for r in ranked], [r.shadow_r for r in ranked])
    numbers = (
        f"ranks 1-5 - 16-20 {_r(value)}, {_ci(interval)}; n {n_top} and {n_bottom}; Spearman "
        f"rank vs R {'n/a' if rho is None else f'{rho:+.3f}'} (H4 expects it negative)"
    )
    need = cfg.min_n_per_side
    if n_top < need or n_bottom < need or interval is None:
        verdict: Verdict = "insufficient data"
    else:
        verdict = "pass" if interval.low > 0 else "fail"
    return Line("H4", "Higher relative-volume ranks earn more (sanity)", verdict, numbers)


def _criteria(
    conn: sqlite3.Connection, epoch: int, s: EpochSummary, through: date, enough: bool
) -> tuple[Line, ...]:
    v = s.veto.v_interval
    c1: Verdict = (
        "insufficient data" if not enough or v is None else ("pass" if v.low > 0 else "fail")
    )
    nets = [
        float((pnl - fees) / (qty * (entry - stop)))
        for pnl, fees, qty, entry, stop in (
            (
                _d(r["pnl"]),
                _d(r["fees_est"]) or 0,
                r["qty"],
                _d(r["entry_price"]),
                _d(r["stop_price"]),
            )
            for r in conn.execute(
                "SELECT pnl, fees_est, qty, entry_price, stop_price FROM trades WHERE book = 'B' "
                "AND eval_epoch = ? AND pnl IS NOT NULL AND substr(entry_ts, 1, 10) <= ?",
                (epoch, through.isoformat()),
            )
        )
        if pnl is not None and entry is not None and stop is not None and qty and entry > stop
    ]
    net_r = sum(nets) / len(nets) if nets else None
    c2: Verdict = (
        "insufficient data" if not enough or net_r is None else ("pass" if net_r > 0 else "fail")
    )
    b = s.books.get("B")
    dd = None if b is None else b.max_drawdown_pct
    c3: Verdict = (
        "insufficient data"
        if not enough or dd is None
        else ("pass" if dd <= MAX_DRAWDOWN_PCT else "fail")
    )
    runs = session_runs(conn, epoch_sessions(conn, epoch, through))
    longest, unattended = longest_clean_run(runs), sum(r.unattended for r in runs)
    c4: Verdict = (
        "pass"
        if longest >= UNATTENDED_SESSIONS
        else ("insufficient data" if len(runs) < MIN_DAYS else "fail")
    )
    return (
        Line(
            "1",
            "V's 90% interval's lower bound is above 0",
            c1,
            "no interval" if v is None else f"lower bound {v.low:+.3f} R",
        ),
        Line(
            "2",
            "Book B's mean R per trade, after estimated fees, is above 0",
            c2,
            "n/a" if net_r is None else f"{net_r:+.3f} R over {_n(len(nets), 'trade', 'trades')}",
        ),
        Line(
            "3",
            f"Book B's max drawdown is at most {MAX_DRAWDOWN_PCT:.0f}%",
            c3,
            "n/a" if dd is None else f"{dd:.2f}%",
        ),
        Line(
            "4",
            f"{UNATTENDED_SESSIONS} consecutive unattended sessions without a P1",
            c4,
            f"longest unattended run without a P1: {_n(longest, 'session', 'sessions')}; "
            f"{unattended} of {_n(len(runs), 'session', 'sessions')} unattended",
        ),
        Line("5", "The human signs off", "for the human", "G7"),
    )


def _d(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _outcome(
    days: int,
    enough: bool,
    s: EpochSummary,
    hypotheses: Sequence[Line],
    criteria: Sequence[Line],
) -> tuple[str, str]:
    v = s.veto.v_interval
    wide = v is not None and v.low <= 0 <= v.high and v.high - v.low > INCONCLUSIVE_WIDTH_R
    # The minimum data counts H1's vetoes, so the row's "fewer than 20 vetoes" is in "not enough".
    if days >= INCONCLUSIVE_DAYS and (not enough or wide):
        return _INCONCLUSIVE
    if not enough:
        return ("keep running", "The minimum data isn't reached yet: run on, then ask again.")
    if wide:
        return ("keep running", "V's interval still spans 0 widely: run on to 90 days.")
    h1 = hypotheses[0].verdict
    a, b = s.books.get("A"), s.books.get("B")
    a_up = a is not None and (a.net_pnl_usd or 0) > 0
    both_down = (
        a is not None and b is not None and (a.net_pnl_usd or 0) < 0 and (b.net_pnl_usd or 0) < 0
    )
    if both_down:
        return ("both books negative", "Stop, or redesign the strategy (the human decides).")
    if h1 == "fail" and a_up:
        return (
            "H1 fails, Book A positive",
            "The LLM adds nothing measurable. Consider running the rules alone, or continuing "
            "paper.",
        )
    checked = [c for c in criteria if c.name in ("1", "2", "3", "4")]
    if all(c.verdict == "pass" for c in checked):
        return (
            "the criteria code can check pass",
            "Criteria 1-4 pass; the go/no-go is the human's (G7).",
        )
    return ("criteria not met", "Live trading isn't recommended. The human decides what next.")


_INCONCLUSIVE = (
    "inconclusive",
    "Not evidence either way. The human chooses, once: extend the epoch by 30 trading days, or "
    "stop and treat H1 as not shown. A second inconclusive memo means H1 is not shown.",
)


# ---------------------------------------------------------------- rendering


_REGISTERED = (
    "Judged by the rules pre-registered in STRATEGY.md section 8. Nothing here trades live: only "
    "the human decides, at G7."
)
_SHAKEDOWN = (
    "Epoch 0 is the build and its shakedown (STRATEGY.md section 9), so this memo is a dry run, "
    "not an evaluation."
)


def _notes(m: Memo) -> list[str]:
    return [_REGISTERED, *([_SHAKEDOWN] if m.epoch == 0 else [])]


def _title(m: Memo) -> str:
    return f"Evaluation memo, epoch {m.epoch}, through {m.through}"


def _sections(m: Memo) -> list[tuple[str, list[list[str]]]]:
    return [
        ("Minimum data before deciding", [[x.statement, x.verdict, x.numbers] for x in m.minimum]),
        ("Hypotheses", [[f"{x.name}: {x.statement}", x.verdict, x.numbers] for x in m.hypotheses]),
        (
            "Recommend live trading only if all hold",
            [[f"{x.name}. {x.statement}", x.verdict, x.numbers] for x in m.criteria],
        ),
    ]


def render_markdown(m: Memo) -> str:
    lines = [
        f"# {_title(m)}",
        "",
        f"- **Outcome: {m.outcome}.** {m.next_step}",
        f"- {counts(m)}.",
        *(f"- {note}" for note in _notes(m)),
    ]
    header = ["what", "verdict", "numbers"]
    for title, rows in _sections(m):
        lines += ["", f"## {title}", "", md_row(header), "|---|---|---|"]
        lines += [md_row(row) for row in rows]
    lines += ["", "## Limitations", ""] + [f"- {item}" for item in LIMITATIONS] + [""]
    return "\n".join(lines)


def render_html(m: Memo) -> str:
    e = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{e(_title(m))}</title>",
        "<style>body{font-family:sans-serif;margin:1.5em}table{border-collapse:collapse;"
        "margin:.5em 0 1.5em}th,td{border:1px solid #ccc;padding:2px 8px;text-align:left}"
        "th{background:#f3f3f3}</style></head><body>",
        f"<h1>{e(_title(m))}</h1>",
        f"<p><b>Outcome: {e(m.outcome)}.</b> {e(m.next_step)}</p>",
        f"<p>{e(counts(m))}.</p>",
        *(f"<p>{e(note)}</p>" for note in _notes(m)),
    ]
    for title, rows in _sections(m):
        parts.append(
            f"<h2>{e(title)}</h2><table><tr><th>what</th><th>verdict</th><th>numbers</th></tr>"
        )
        parts += ["<tr>" + "".join(f"<td>{e(c)}</td>" for c in row) + "</tr>" for row in rows]
        parts.append("</table>")
    parts.append("<h2>Limitations</h2><ul>")
    parts += [f"<li>{e(item)}</li>" for item in LIMITATIONS]
    parts.append("</ul></body></html>")
    return "\n".join(parts)


def write_memo(m: Memo, reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    md = reports_dir / f"evaluation-epoch-{m.epoch}.md"
    page = reports_dir / f"evaluation-epoch-{m.epoch}.html"
    md.write_text(render_markdown(m), encoding="utf-8")
    page.write_text(render_html(m), encoding="utf-8")
    return md, page
