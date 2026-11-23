"""The post-close review's content (S§6.1, S§6.5-6.6, A§4.8; the Phase 4 plan's decision 11;
DECISIONS D-071).

**What the review is about.** Code picks the targets (``select_targets``), so coverage doesn't
depend on what Claude looks at. All come from the day's top ``top_n`` signals, never a short:

1. every signal either book traded, wins included (``traded``);
2. every triggered long Book B vetoed (``b_vetoed``). Triggered counts the SIP bars too: a long
   only SIP saw trigger got no decision from Book B, and its veto still counts;
3. up to ``notable_untraded`` other triggered longs, the largest |shadow R| first (``notable``).

At most 20, in that order, each group by rank (the notable by |R|), so a trim drops the notable
first.

**When each target was decided** (``decision_point``): when the pack behind Book B's verdict was
sent, the first attempt of that call; else the live trigger; else the arming. Book B's verdict
is the one its decision rests on, else the day's latest. A target's news is stamped against
that time: at most 3 items from before it and 2 from after, nearest first, each read when the
engine could read it (the later of its creation and its receipt; D-063).

**``DailyReviewWork``** is ``ports.ReviewWork``: the pack, built from one read of the journal;
``persist``, the transaction that stores a good answer; and ``not_reviewed``.

**Storing an answer** (``persist_review``), in the attempt's own transaction (decision 11):

- ``daily_reviews``, with the targets asked about;
- one ``signal_reviews`` row per target: a review of anything else, or a second of the same
  target, is dropped, and repeated factors are kept once;
- process issues and notes: one linking an open item by id bumps its count and last-seen date
  and keeps its first text; today's text goes to ``review_mentions``, as every raising's does.
  One review links an item once. An id that is unknown, or names a closed item, opens a new one;
- lesson proposals, as ``proposed`` (amends P5.2), with the day that proposed them.

**Follow-ups:** only the human closes an issue (``fixed``, ``wontfix``) or a note (``accepted``,
``rejected``, ``done``), with a response (``set_followup``; ``trader followups``).
"""

import asyncio
import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

import structlog
from pydantic import BaseModel

from trader.config import Settings
from trader.core.clock import Clock, ensure_et, et_datetime, fmt_iso, parse_et_iso
from trader.core.models import NewsItem, Signal
from trader.data.news import news_from_row
from trader.llm.client import LLMResult
from trader.llm.context import (
    REVIEW_MAX_TARGETS,
    BadProcess,
    BookResult,
    Common,
    FactorCount,
    History,
    IncidentView,
    LessonView,
    OpenIssue,
    OpenNote,
    Pack,
    ReviewNews,
    ReviewSignal,
    ReviewTarget,
    review_pack,
    role_limits,
)
from trader.llm.schemas import DailyReview, SymbolAssessment
from trader.memory.epochs import current_epoch
from trader.memory.journal import Journal, db_to_dec
from trader.memory.shadow import ShadowOutcome
from trader.memory.stats import Bootstrap, VetoValue, read_epoch, veto_value
from trader.memory.trades import TradeRow, trade_from_row
from trader.memory.verdict_view import standing_for_day, standing_verdicts
from trader.strategy.orb_sip import signal_from_row

log = structlog.get_logger(__name__)

SelectedFor = Literal["traded", "b_vetoed", "notable"]
Basis = Literal["premarket_pack", "vet_pack", "trigger", "armed"]
NEWS_BEFORE = 3  # a target's news from before its decision (the plan's P4.10)
NEWS_AFTER = 2
# A failed review whose answer came but didn't hold up is "invalid"; any other is "missing".
INVALID_FAILURES = frozenset(
    {
        "invalid_json",
        "invalid_envelope",
        "no_structured_output",
        "schema_invalid",
        "wrong_pack_id",
        "output_too_large",
    }
)
ISSUE_STATUSES = ("fixed", "wontfix")
NOTE_STATUSES = ("accepted", "rejected", "done")
FollowupKind = Literal["issue", "note"]


# ---------------------------------------------------------------- the day's signals


@dataclass(frozen=True)
class VerdictRow:
    """Book B's verdict on a signal, and when the pack behind it was sent."""

    assessment: SymbolAssessment
    source: Literal["premarket", "vet"]
    llm_call_id: str
    sent_at: datetime | None


@dataclass(frozen=True)
class DaySignal:
    """One of the day's signals with everything the review reads about it."""

    signal: Signal
    shadow: ShadowOutcome | None = None
    sip_only: bool = False
    decisions: Mapping[str, str] = field(default_factory=dict)  # book -> final reason code
    trades: Mapping[str, TradeRow] = field(default_factory=dict)  # book -> its trade
    verdict: VerdictRow | None = None  # Book B's

    @property
    def triggered(self) -> bool:
        """Live, or by the SIP bars (the shadow)."""
        return self.signal.triggered_at is not None or (
            self.shadow is not None and self.shadow.triggered
        )


@dataclass(frozen=True)
class Picked:
    day: DaySignal
    selected_for: SelectedFor


def _rank(s: DaySignal) -> tuple[int, str]:
    return s.signal.rel_vol_rank, s.signal.symbol


def select_targets(
    signals: Sequence[DaySignal], *, top_n: int, notable: int, cap: int = REVIEW_MAX_TARGETS
) -> list[Picked]:
    """S§6.5's targets, in the module docstring's order."""
    longs = sorted(
        (s for s in signals if s.signal.direction == "long" and s.signal.rel_vol_rank <= top_n),
        key=_rank,
    )
    traded = [s for s in longs if s.trades]
    vetoed = [s for s in longs if not s.trades and _b_vetoed(s)]
    taken = {s.signal.id for s in traded + vetoed}
    others = sorted(
        (
            s
            for s in longs
            if s.signal.id not in taken
            and s.triggered
            and s.shadow is not None
            and s.shadow.shadow_r is not None
        ),
        key=lambda s: (-abs(_shadow_r(s)), *_rank(s)),
    )
    picked = (
        [Picked(s, "traded") for s in traded]
        + [Picked(s, "b_vetoed") for s in vetoed]
        + [Picked(s, "notable") for s in others[: max(notable, 0)]]
    )
    return picked[: max(cap, 0)]


def _shadow_r(s: DaySignal) -> float:
    return (s.shadow.shadow_r if s.shadow is not None else None) or 0.0


def _b_vetoed(s: DaySignal) -> bool:
    if s.decisions.get("B") == "vetoed":
        return True
    vetoed = s.verdict is not None and s.verdict.assessment.verdict == "veto"
    return "B" not in s.decisions and vetoed and s.triggered


def decision_point(s: DaySignal) -> tuple[datetime, Basis] | None:
    """When the target was decided, and on what (the module docstring)."""
    v = s.verdict
    if v is not None and v.sent_at is not None:
        return v.sent_at, "premarket_pack" if v.source == "premarket" else "vet_pack"
    if s.signal.triggered_at is not None:
        return s.signal.triggered_at, "trigger"
    if s.signal.armed_at is not None:
        return s.signal.armed_at, "armed"
    if s.shadow is not None and s.shadow.trigger_ts is not None:
        return s.shadow.trigger_ts, "trigger"
    return None


def target_news(
    symbol: str,
    stored: Sequence[tuple[NewsItem, datetime | None]],
    *,
    decided: datetime,
    as_of: datetime,
    lookback: timedelta,
) -> list[ReviewNews]:
    """The target's news: the ``NEWS_BEFORE`` items nearest before its decision (within
    ``lookback`` of it) and the ``NEWS_AFTER`` nearest after, up to ``as_of``."""
    before: list[tuple[datetime, ReviewNews]] = []
    after: list[tuple[datetime, ReviewNews]] = []
    for item, received in stored:
        if symbol not in item.symbols or item.created_at > as_of:
            continue
        seen = max(item.created_at, received or item.created_at)
        entry = (seen, ReviewNews(item, received_at=received))
        if seen <= decided:
            if item.created_at >= decided - lookback:
                before.append(entry)
        else:
            after.append(entry)
    before.sort(key=lambda p: (p[0], p[1].item.id), reverse=True)
    after.sort(key=lambda p: (p[0], p[1].item.id))
    return [n for _, n in before[:NEWS_BEFORE]] + [n for _, n in after[:NEWS_AFTER]]


# ---------------------------------------------------------------- reading the journal


@dataclass(frozen=True)
class _Gathered:
    signals: list[DaySignal]
    news: list[tuple[NewsItem, datetime | None]]
    books: dict[str, BookResult]
    incidents: list[IncidentView]
    stats: dict[str, float | int | None]
    lessons: list[LessonView]
    history: History


def _gather(
    conn: sqlite3.Connection,
    day: date,
    as_of: datetime,
    *,
    lookback: timedelta,
    history_days: int,
    boot: Bootstrap | None = None,
) -> _Gathered:
    d = day.isoformat()
    signals = [
        signal_from_row(r)
        for r in conn.execute(
            "SELECT * FROM signals WHERE date = ? ORDER BY rel_vol_rank, id", (d,)
        )
    ]
    ids = {s.id for s in signals}
    decisions: dict[str, dict[str, str]] = defaultdict(dict)
    for r in conn.execute(
        "SELECT f.signal_id, f.book, f.reason_code FROM final_decisions f "
        "JOIN signals s ON s.id = f.signal_id WHERE s.date = ?",
        (d,),
    ):
        decisions[r["signal_id"]][r["book"]] = r["reason_code"]
    trades: dict[str, dict[str, TradeRow]] = defaultdict(dict)
    for r in conn.execute("SELECT * FROM trades WHERE entry_ts LIKE ? ORDER BY id", (f"{d}T%",)):
        t = trade_from_row(r)
        if t.signal_id in ids:
            trades[t.signal_id][t.book] = t
    shadows: dict[str, tuple[ShadowOutcome, bool]] = {}
    by_id = {s.id: s for s in signals}
    for r in conn.execute("SELECT * FROM shadow_outcomes"):
        sig = by_id.get(r["signal_id"])
        if sig is not None:
            shadows[sig.id] = (_shadow(r, sig), bool(r["sip_only_trigger"]))
    verdicts = _b_verdicts(conn, day)
    out = []
    for sig in signals:
        shadow, sip_only = shadows.get(sig.id, (None, False))
        out.append(
            DaySignal(
                signal=sig,
                shadow=shadow,
                sip_only=sip_only,
                decisions=dict(decisions.get(sig.id, {})),
                trades=dict(trades.get(sig.id, {})),
                verdict=verdicts.get(sig.symbol),
            )
        )
    since = et_datetime(day, time(0)) - lookback
    news = [
        (news_from_row(r), parse_et_iso(r["received_at"]) if r["received_at"] else None)
        for r in conn.execute(
            "SELECT * FROM news WHERE created_at >= ? AND created_at <= ? ORDER BY created_at, id",
            (fmt_iso(since), fmt_iso(as_of)),
        )
    ]
    return _Gathered(
        signals=out,
        news=news,
        books=_books(conn, day),
        incidents=[
            IncidentView(
                parse_et_iso(r["ts"]),
                r["severity"] or "",
                r["component"] or "",
                r["code"] or "",
                r["message"] or "",
            )
            for r in conn.execute(
                "SELECT ts, severity, component, code, message FROM incidents WHERE ts LIKE ? "
                "ORDER BY id",
                (f"{d}T%",),
            )
        ],
        stats=epoch_stats(conn, current_epoch(conn), day, boot=boot),
        lessons=_lessons(conn),
        history=read_history(conn, day, days=history_days),
    )


def _shadow(r: sqlite3.Row, sig: Signal) -> ShadowOutcome:
    return ShadowOutcome(
        direction="long" if sig.direction == "long" else "short",
        exit_reason=r["exit_reason"],
        trigger_ts=parse_et_iso(r["trigger_ts"]) if r["trigger_ts"] else None,
        entry_price=db_to_dec(r["entry_price"]),
        exit_price=db_to_dec(r["exit_price"]),
        shadow_r=r["shadow_r"],
        mfe_r=r["mfe_r"],
        mae_r=r["mae_r"],
        minutes_held=r["minutes_held"],
        trigger_resolution=r["trigger_resolution"],
        spy_move_pct=r["spy_move_pct"],
    )


def _b_verdicts(conn: sqlite3.Connection, day: date) -> dict[str, VerdictRow]:
    """Book B's standing verdict per symbol (``verdict_view``), with when its pack was sent."""
    sent = {
        r["logical_id"]: parse_et_iso(r["sent"])
        for r in conn.execute(
            "SELECT logical_id, MIN(started_at) AS sent FROM llm_calls WHERE date = ? "
            "AND purpose = 'trading' AND started_at IS NOT NULL GROUP BY logical_id",
            (day.isoformat(),),
        )
    }
    return {
        symbol: VerdictRow(
            assessment=SymbolAssessment(
                symbol=symbol,
                catalyst_type=r["catalyst_type"],
                catalyst_fresh=bool(r["catalyst_fresh"]),
                catalyst_confirmed=bool(r["catalyst_confirmed"]),
                risk_flags=json.loads(r["risk_flags_json"] or "[]"),
                verdict=r["verdict"],
                confidence=float(r["confidence"]),
                reason=r["reason"] or "",
            ),
            source="premarket" if r["source"] == "premarket" else "vet",
            llm_call_id=r["llm_call_id"],
            sent_at=sent.get(r["logical_id"]),
        )
        for symbol, r in standing_for_day(conn, day).items()
    }


def _books(conn: sqlite3.Connection, day: date) -> dict[str, BookResult]:
    out: dict[str, BookResult] = {}
    for r in conn.execute(
        "SELECT * FROM daily_stats WHERE date = ? ORDER BY book", (day.isoformat(),)
    ):
        out[r["book"]] = BookResult(
            start_equity=db_to_dec(r["start_equity"]),
            end_equity=db_to_dec(r["end_equity"]),
            pnl=db_to_dec(r["pnl"]),
            trades=int(r["n_trades"] or 0),
        )
    return out


def running_stats(
    *,
    epoch: int,
    days: int,
    a_r: Sequence[float],
    b_r: Sequence[float],
    shadow_r: Sequence[float],
    vetoed_r: Sequence[float],
    b_vetoed_triggers: int,
    b_veto_verdicts: int,
) -> dict[str, float | int | None]:
    """The review's ``stats``: each book's trades, mean R and win rate; the triggered longs'
    shadow R; Book B's vetoes and the shadow R they turned down (D-073):
    - ``b_vetoed_triggers``: Book B's ``vetoed`` decisions, each a veto of a signal that
      triggered on the live feed, and ``b_vetoed_mean_shadow_r`` their mean shadow R;
    - ``b_veto_verdicts``: the longs whose standing verdict is a veto, triggered or not."""
    out: dict[str, float | int | None] = {"epoch": epoch, "days": days}
    for key, rs in (("a", a_r), ("b", b_r)):
        out[f"{key}_trades"] = len(rs)
        out[f"{key}_mean_r"] = _mean(rs)
        out[f"{key}_win_rate"] = (sum(r > 0 for r in rs) / len(rs)) if rs else None
    out["shadow_longs"] = len(shadow_r)
    out["shadow_mean_r"] = _mean(shadow_r)
    out["b_vetoed_triggers"] = b_vetoed_triggers
    out["b_vetoed_mean_shadow_r"] = _mean(vetoed_r)
    out["b_veto_verdicts"] = b_veto_verdicts
    return out


def veto_stats(veto: VetoValue) -> dict[str, float | int | None]:
    """The veto value's numbers for the review's ``stats`` (``memory/stats.py``; D-075): V, its
    interval and each side's n over the triggered longs with a standing verdict, and the vetoed
    side's own mean shadow R."""
    interval = veto.v_interval
    return {
        "v_r": veto.v,
        "v_ci_low": None if interval is None else interval.low,
        "v_ci_high": None if interval is None else interval.high,
        "v_n_allowed": veto.n_allowed,
        "v_n_vetoed": veto.n_vetoed,
        "v_vetoed_mean_r": veto.mean_vetoed,
    }


def epoch_stats(
    conn: sqlite3.Connection, epoch: int, day: date, *, boot: Bootstrap | None = None
) -> dict[str, float | int | None]:
    """The epoch's running numbers, through ``day`` (S§6.1's rolling stats); with ``boot``, the
    veto value and its interval too (``veto_stats``)."""
    d = day.isoformat()
    row = conn.execute(
        "SELECT COUNT(DISTINCT date) FROM daily_stats WHERE eval_epoch = ? AND date <= ?",
        (epoch, d),
    ).fetchone()
    books: dict[str, list[float]] = {}
    for book in ("A", "B"):
        books[book] = [
            r[0]
            for r in conn.execute(
                "SELECT r_multiple FROM trades WHERE book = ? AND eval_epoch = ? "
                "AND substr(entry_ts, 1, 10) <= ? AND r_multiple IS NOT NULL",
                (book, epoch, d),
            )
        ]
    shadow = [
        r[0]
        for r in conn.execute(
            "SELECT o.shadow_r FROM shadow_outcomes o JOIN signals s ON s.id = o.signal_id "
            "WHERE s.eval_epoch = ? AND s.date <= ? AND s.direction = 'long' "
            "AND o.shadow_r IS NOT NULL",
            (epoch, d),
        )
    ]
    vetoed = [
        r[0]
        for r in conn.execute(
            "SELECT o.shadow_r FROM final_decisions f JOIN signals s ON s.id = f.signal_id "
            "LEFT JOIN shadow_outcomes o ON o.signal_id = s.id WHERE f.book = 'B' "
            "AND f.reason_code = 'vetoed' AND s.eval_epoch = ? AND s.date <= ?",
            (epoch, d),
        )
    ]
    longs = {
        (r["date"], r["symbol"])
        for r in conn.execute(
            "SELECT date, symbol FROM signals WHERE eval_epoch = ? AND date <= ? "
            "AND direction = 'long'",
            (epoch, d),
        )
    }
    first = min((date.fromisoformat(when) for when, _ in longs), default=day)
    standing = standing_verdicts(conn, first=first, last=day)
    out = running_stats(
        epoch=epoch,
        days=int(row[0]),
        a_r=books["A"],
        b_r=books["B"],
        shadow_r=shadow,
        vetoed_r=[r for r in vetoed if r is not None],
        b_vetoed_triggers=len(vetoed),
        b_veto_verdicts=sum(
            1 for key, v in standing.items() if key in longs and v["verdict"] == "veto"
        ),
    )
    if boot is not None:
        out |= veto_stats(veto_value(read_epoch(conn, epoch, last=day).longs, boot))
    return out


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _lessons(conn: sqlite3.Connection) -> list[LessonView]:
    """Promoted first, then the newest proposals (D-063's "For P4.10")."""
    rows = conn.execute(
        "SELECT * FROM lessons WHERE status IN ('promoted', 'proposed') ORDER BY "
        "status = 'promoted' DESC, COALESCE(promoted_at, created_at) DESC, id DESC"
    ).fetchall()
    out = []
    for r in rows:
        try:
            query = json.loads(r["evidence_query_json"] or "{}")
        except ValueError:
            query = {}
        ci = None if r["ci_low"] is None or r["ci_high"] is None else (r["ci_low"], r["ci_high"])
        out.append(
            LessonView(
                id=r["id"],
                status=r["status"],
                statement=r["statement"] or "",
                evidence_query=query if isinstance(query, dict) else {},
                claimed_direction=r["claimed_direction"] or "",
                suggested_use=r["suggested_use"] or "",
                n_subset=r["n_subset"],
                n_complement=r["n_complement"],
                effect_r=r["effect_r"],
                ci=ci,
            )
        )
    return out


def read_history(conn: sqlite3.Connection, day: date, *, days: int) -> History:
    """S§6.6: what the review sees of earlier days, each list newest first."""
    d = day.isoformat()
    summaries = [
        (date.fromisoformat(r["date"]), r["summary"] or "")
        for r in conn.execute(
            "SELECT date, summary FROM daily_reviews WHERE status = 'ok' AND date < ? "
            "ORDER BY date DESC LIMIT ?",
            (d, max(days, 0)),
        )
    ]
    bad: list[BadProcess] = []
    if summaries:
        since = summaries[-1][0].isoformat()
        bad = [
            BadProcess(
                date.fromisoformat(r["date"]),
                r["signal_id"],
                r["symbol"],
                r["why"] or "",
                r["b_note"] or "",
            )
            for r in conn.execute(
                "SELECT r.date, r.signal_id, s.symbol, r.why, r.b_note FROM signal_reviews r "
                "JOIN signals s ON s.id = r.signal_id WHERE r.b_process = 'bad_process' "
                "AND r.date >= ? AND r.date < ? ORDER BY r.date DESC, s.rel_vol_rank, s.id",
                (since, d),
            )
        ]
    return History(
        summaries=summaries,
        bad_process=bad,
        factor_tally=factor_tally(conn, current_epoch(conn), day),
        open_issues=[
            OpenIssue(
                r["id"],
                r["severity"] or "",
                r["component"] or "",
                r["description"] or "",
                date.fromisoformat(r["first_seen"]),
                date.fromisoformat(r["last_seen"]),
                int(r["occurrences"] or 0),
            )
            for r in conn.execute(
                "SELECT * FROM process_issues WHERE status = 'open' "
                "ORDER BY last_seen DESC, id DESC"
            )
        ],
        open_notes=[
            OpenNote(
                r["id"],
                r["note"] or "",
                date.fromisoformat(r["first_raised"]),
                date.fromisoformat(r["last_raised"]),
                int(r["times_raised"] or 0),
            )
            for r in conn.execute(
                "SELECT * FROM review_notes WHERE status = 'open' "
                "ORDER BY last_raised DESC, id DESC"
            )
        ],
    )


def factor_tally(conn: sqlite3.Connection, epoch: int, day: date) -> list[FactorCount]:
    """The epoch's factors before ``day``: targets carrying each, their wins and losses by the
    sign of shadow R, its mean, and how many were knowable at the decision. Most carried first."""
    counts: dict[str, list[Any]] = {}
    for r in conn.execute(
        "SELECT r.factors_json, r.knowable_at_decision, o.shadow_r FROM signal_reviews r "
        "LEFT JOIN shadow_outcomes o ON o.signal_id = r.signal_id "
        "WHERE r.eval_epoch = ? AND r.date < ?",
        (epoch, day.isoformat()),
    ):
        try:
            factors = json.loads(r["factors_json"] or "[]")
        except ValueError:
            continue
        for factor in dict.fromkeys(f for f in factors if isinstance(f, str)):
            c = counts.setdefault(factor, [0, 0, 0, [], 0])
            c[0] += 1
            shadow_r = r["shadow_r"]
            if shadow_r is not None:
                c[1] += shadow_r > 0
                c[2] += shadow_r < 0
                c[3].append(shadow_r)
            c[4] += r["knowable_at_decision"] == "yes"
    tally = [
        FactorCount(factor, c[0], c[1], c[2], _mean(c[3]), c[4]) for factor, c in counts.items()
    ]
    return sorted(tally, key=lambda f: (-f.targets, f.factor))


# ---------------------------------------------------------------- storing the answer


def persist_review(
    conn: sqlite3.Connection, review: DailyReview, *, day: date, attempt_id: str, now: datetime
) -> None:
    """The answer, in the attempt's transaction (the module docstring)."""
    d, at = day.isoformat(), fmt_iso(now)
    epoch = current_epoch(conn)
    targets = _targets_of(conn, attempt_id)
    conn.execute(
        "INSERT OR REPLACE INTO daily_reviews (date, status, summary, llm_call_id, eval_epoch, "
        "created_at, failure, targets_json) VALUES (?, 'ok', ?, ?, ?, ?, NULL, ?)",
        (d, review.summary, attempt_id, epoch, at, json.dumps(targets)),
    )
    reviewed: set[str] = set()
    for r in review.signal_reviews:
        if r.signal_id not in targets or r.signal_id in reviewed:
            continue
        reviewed.add(r.signal_id)
        conn.execute(
            "INSERT OR REPLACE INTO signal_reviews (signal_id, date, went_well, went_badly, why, "
            "factors_json, knowable_at_decision, b_process, b_note, llm_call_id, eval_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r.signal_id,
                d,
                r.went_well,
                r.went_badly,
                r.why,
                json.dumps(list(dict.fromkeys(r.factors))),
                r.knowable_at_decision,
                r.b_process,
                r.b_note,
                attempt_id,
                epoch,
            ),
        )
    linked: set[int] = set()
    for issue in review.process_issues:
        item = _open(conn, "process_issues", issue.existing_issue_id)
        if item is not None:
            if item in linked:
                continue
            linked.add(item)
            conn.execute(
                "UPDATE process_issues SET occurrences = occurrences + 1, last_seen = ?, "
                "last_llm_call_id = ? WHERE id = ?",
                (d, attempt_id, item),
            )
        else:
            item = _insert(
                conn,
                "INSERT INTO process_issues (first_seen, last_seen, occurrences, severity, "
                "component, description, status, last_llm_call_id) "
                "VALUES (?, ?, 1, ?, ?, ?, 'open', ?)",
                (d, d, issue.severity, issue.component, issue.description, attempt_id),
            )
        _mention(
            conn,
            d,
            "issue",
            item,
            issue.description,
            attempt_id,
            severity=issue.severity,
            component=issue.component,
        )
    linked = set()
    for note in review.human_notes:
        item = _open(conn, "review_notes", note.existing_note_id)
        if item is not None:
            if item in linked:
                continue
            linked.add(item)
            conn.execute(
                "UPDATE review_notes SET times_raised = times_raised + 1, last_raised = ?, "
                "last_llm_call_id = ? WHERE id = ?",
                (d, attempt_id, item),
            )
        else:
            item = _insert(
                conn,
                "INSERT INTO review_notes (first_raised, last_raised, times_raised, note, status, "
                "last_llm_call_id) VALUES (?, ?, 1, ?, 'open', ?)",
                (d, d, note.note, attempt_id),
            )
        _mention(conn, d, "note", item, note.note, attempt_id)
    for p in review.lesson_proposals:
        conn.execute(
            "INSERT INTO lessons (id, created_at, source_call_id, statement, rationale, "
            "evidence_query_json, claimed_direction, suggested_use, status, source_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)",
            (
                _next_lesson_id(conn),
                at,
                attempt_id,
                p.statement,
                p.rationale,
                json.dumps(p.evidence_query.model_dump(mode="json"), sort_keys=True),
                p.claimed_direction,
                p.suggested_use,
                d,
            ),
        )


def record_missing(
    conn: sqlite3.Connection,
    *,
    day: date,
    status: Literal["missing", "invalid"],
    failure: str,
    call_id: str | None,
    now: datetime,
) -> None:
    """A review that didn't come, or came invalid: S§6.1's ``daily_reviews.status``."""
    targets = _targets_of(conn, call_id) if call_id else None
    conn.execute(
        "INSERT OR REPLACE INTO daily_reviews (date, status, summary, llm_call_id, eval_epoch, "
        "created_at, failure, targets_json) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
        (
            day.isoformat(),
            status,
            call_id,
            current_epoch(conn),
            fmt_iso(now),
            failure[:200],
            None if targets is None else json.dumps(targets),
        ),
    )


def _targets_of(conn: sqlite3.Connection, attempt_id: str) -> list[str]:
    """The targets the attempt's pack held, in order (its ``candidates_json``)."""
    row = conn.execute(
        "SELECT candidates_json FROM llm_calls WHERE id = ?", (attempt_id,)
    ).fetchone()
    if row is None or not row[0]:
        return []
    found = json.loads(row[0])
    return [str(t) for t in found] if isinstance(found, list) else []


def _open(conn: sqlite3.Connection, table: str, item_id: int | None) -> int | None:
    """``item_id`` when it names an open row of ``table``; None opens a new one."""
    if item_id is None:
        return None
    row = conn.execute(f"SELECT id FROM {table} WHERE id = ? AND status = 'open'", (item_id,))
    found = row.fetchone()
    return int(found[0]) if found is not None else None


def _insert(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
    rowid = conn.execute(sql, params).lastrowid
    assert rowid is not None
    return rowid


def _mention(
    conn: sqlite3.Connection,
    day: str,
    kind: FollowupKind,
    item: int,
    text: str,
    call_id: str,
    *,
    severity: str | None = None,
    component: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO review_mentions (date, kind, item_id, severity, component, text, "
        "llm_call_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (day, kind, item, severity, component, text, call_id),
    )


def _next_lesson_id(conn: sqlite3.Connection) -> str:
    """``L-001`` and on: one past the highest numbered id."""
    top = 0
    for (lesson_id,) in conn.execute("SELECT id FROM lessons WHERE id LIKE 'L-%'"):
        tail = str(lesson_id)[2:]
        if tail.isdigit():
            top = max(top, int(tail))
    return f"L-{top + 1:03d}"


# ---------------------------------------------------------------- follow-ups


@dataclass(frozen=True)
class Followup:
    kind: FollowupKind
    id: int
    status: str
    first: date
    last: date
    count: int
    text: str  # a model's
    severity: str = ""
    component: str = ""  # a model's
    response: str = ""  # the human's


def followups(conn: sqlite3.Connection, *, include_closed: bool = False) -> list[Followup]:
    """Process issues, then notes: the oldest and most recurring first."""
    where = "" if include_closed else " WHERE status = 'open'"
    out = [
        Followup(
            "issue",
            r["id"],
            r["status"] or "",
            date.fromisoformat(r["first_seen"]),
            date.fromisoformat(r["last_seen"]),
            int(r["occurrences"] or 0),
            r["description"] or "",
            r["severity"] or "",
            r["component"] or "",
            r["resolution"] or "",
        )
        for r in conn.execute(
            f"SELECT * FROM process_issues{where} ORDER BY first_seen, occurrences DESC, id"
        )
    ]
    out += [
        Followup(
            "note",
            r["id"],
            r["status"] or "",
            date.fromisoformat(r["first_raised"]),
            date.fromisoformat(r["last_raised"]),
            int(r["times_raised"] or 0),
            r["note"] or "",
            response=r["human_response"] or "",
        )
        for r in conn.execute(
            f"SELECT * FROM review_notes{where} ORDER BY first_raised, times_raised DESC, id"
        )
    ]
    return out


class FollowupError(ValueError):
    pass


def set_followup(
    conn: sqlite3.Connection,
    kind: FollowupKind,
    item_id: int,
    status: str,
    response: str,
    now: datetime,
) -> None:
    """The human's decision on an issue or a note (only the human closes them, S§6.6)."""
    if kind == "issue":
        if status not in ISSUE_STATUSES:
            raise FollowupError(f"an issue's status is one of {', '.join(ISSUE_STATUSES)}")
        sql = "UPDATE process_issues SET status = ?, resolution = ?, resolved_at = ? WHERE id = ?"
    else:
        if status not in NOTE_STATUSES:
            raise FollowupError(f"a note's status is one of {', '.join(NOTE_STATUSES)}")
        sql = "UPDATE review_notes SET status = ?, human_response = ?, decided_at = ? WHERE id = ?"
    if conn.execute(sql, (status, response, fmt_iso(now), item_id)).rowcount != 1:
        raise FollowupError(f"no {kind} {item_id}")


# ---------------------------------------------------------------- ports.ReviewWork


class DailyReviewWork:
    """The review's content for ``BookBJobs`` (``ports.ReviewWork``; the module docstring)."""

    def __init__(self, *, journal: Journal, clock: Clock, settings: Settings, day: date) -> None:
        self._journal, self._clock, self._settings, self._day = journal, clock, settings, day

    async def pack(self, *, as_of: datetime, common: Common) -> Pack | None:
        """The review's pack, or None on a day with no signals: nothing to review."""
        settings, day = self._settings, self._day
        limits = role_limits(settings, "review")
        news = settings.news
        lookback = timedelta(hours=max(news.premarket_lookback_hours, news.vet_lookback_hours))
        boot = Bootstrap.of(settings.lessons)
        got = await self._journal.read(
            lambda conn: _gather(
                conn,
                day,
                ensure_et(as_of),
                lookback=lookback,
                history_days=limits.history_days,
                boot=boot,
            )
        )
        if not got.signals:
            return None

        def build() -> Pack:
            picked = select_targets(
                got.signals,
                top_n=settings.strategy.selection.top_n,
                notable=limits.notable_untraded,
            )
            targets = []
            for p in picked:
                point = decision_point(p.day)
                if point is None:
                    continue
                decided, basis = point
                s = p.day
                targets.append(
                    ReviewTarget(
                        signal_id=s.signal.id,
                        symbol=s.signal.symbol,
                        selected_for=p.selected_for,
                        decision_at=decided,
                        decision_basis=basis,
                        shadow=s.shadow,
                        spy_move_pct=None if s.shadow is None else s.shadow.spy_move_pct,
                        news=target_news(
                            s.signal.symbol,
                            got.news,
                            decided=decided,
                            as_of=as_of,
                            lookback=lookback,
                        ),
                    )
                )
            return review_pack(
                as_of=as_of,
                common=common,
                books=got.books,
                signals=[
                    ReviewSignal(
                        signal=s.signal,
                        shadow=s.shadow,
                        sip_only_trigger=s.sip_only,
                        decisions=s.decisions,
                        trades=s.trades,
                        verdict=None if s.verdict is None else s.verdict.assessment,
                        verdict_source=None if s.verdict is None else s.verdict.source,
                    )
                    for s in got.signals
                ],
                targets=targets,
                incidents=got.incidents,
                stats=got.stats,
                lessons=got.lessons,
                history=got.history,
                limits=limits,
            )

        if self._journal.inline:  # beside simdrive's inline journal, as the packs do (D-069)
            return build()
        return await asyncio.to_thread(build)

    def persist(self, conn: sqlite3.Connection, output: BaseModel, attempt_id: str) -> None:
        if not isinstance(output, DailyReview):
            raise TypeError(f"the review answered with a {type(output).__name__}")
        persist_review(conn, output, day=self._day, attempt_id=attempt_id, now=self._clock.now())

    async def not_reviewed(self, *, why: str, result: LLMResult[Any] | None) -> None:
        status: Literal["missing", "invalid"] = (
            "invalid" if result is not None and result.failure in INVALID_FAILURES else "missing"
        )
        call_id = None if result is None else result.call_id
        now = self._clock.now()
        await self._journal.write(
            lambda conn: record_missing(
                conn, day=self._day, status=status, failure=why, call_id=call_id, now=now
            )
        )
        log.warning("no review today", status=status, why=why)
