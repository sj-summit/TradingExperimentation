"""Lessons (STRATEGY.md §6.2 to 6.3; the Phase 5 plan's P5.2, decisions 10 and 11; DECISIONS D-076).

A lesson starts as a review's proposal, which P4.10 stores as ``proposed``: a statement, and an
evidence query over features recorded before the outcome. Code tests it; a model never does.

**The query** (``validate``):
- the schema's whitelist (S§6.2), and each field's type on top of it:
  - numbers: ``eq ne gt gte lt lte`` with a finite number, never a bool;
  - categories: ``eq ne`` with one of the field's values, or ``in`` with 1 to 10 of them;
  - ``event_day``: ``eq ne`` with true or false;
  - ``risk_flags``: ``contains`` with one risk flag, and nothing else.
- 1 to 4 filters, compared with their complement on ``shadow_r``, and no other key.
- A query is applied in Python to feature rows: no SQL is ever built from one.

**The rows** (``read_features``): the epoch's longs whose shadow triggered, with a shadow R.
- Each carries the S§6.2 fields as the engine knew them before the outcome. Book B's fields
  (catalyst, flags, verdict, its source, confidence) come from the standing verdict
  (``verdict_view``).
- ``price`` is the trigger price, and ``minutes_to_trigger`` the whole minutes from the arming to
  the SIP trigger minute (never below 0).
- A field the engine didn't have, such as a verdict on a long Book B never judged, is unknown,
  and a row is left out of any query that names one of its unknown fields.

**The test** (``judge``; the human's decision, 2026-11-21: only later signals count):
- only rows dated after the proposal's day count: never the data that suggested the lesson;
- the subset is the rows meeting every filter, the complement the rest;
- the effect is mean(subset) minus mean(complement), with an interval from resampled days
  (``memory/stats.py``).
- **One look:** a proposal waits, ``proposed``, until each side has ``min_n_per_side`` rows, and
  is then tested once.
  - It passes if |effect| ≥ ``min_effect_r`` and the interval lies wholly on the claimed side
    of 0.
  - Passing makes it ``promoted``, failing makes it ``rejected``: it is never tested again, so
    repeated looks can't promote a lesson by chance.
- **Demotion:** the first evaluation of each calendar month re-tests promoted lessons on all
  their later rows. An interval that now reaches 0 makes the lesson ``demoted``. Other runs
  refresh a promoted lesson's numbers and leave its status alone.
- Rejected and demoted lessons stay in the table for audit, with ``status_reason``.
"""

import json
import math
import operator
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal, get_args

from trader.config import LessonsConfig
from trader.core.clock import fmt_iso, parse_et_iso
from trader.llm.schemas import CatalystType, DslField, DslOp, RiskFlag, Verdict
from trader.memory.epochs import current_epoch
from trader.memory.stats import Bootstrap, Interval, difference_interval
from trader.memory.verdict_view import standing_verdicts

Status = Literal["proposed", "promoted", "rejected", "demoted"]

NUMBERS = frozenset(
    {"confidence", "gap_pct", "rel_vol", "rel_vol_rank", "or_range_atr", "or_return_pct",
     "price", "atr_pct", "minutes_to_trigger"}
)  # fmt: skip
CATEGORIES: Mapping[str, frozenset[str]] = {
    "catalyst_type": frozenset(get_args(CatalystType)),
    "verdict": frozenset(get_args(Verdict)),
    "verdict_source": frozenset({"premarket", "vet"}),
    "regime_spy_trend": frozenset({"up", "down"}),
    "regime_vol_bucket": frozenset({"low", "mid", "high"}),
    "day_of_week": frozenset({"Mon", "Tue", "Wed", "Thu", "Fri"}),
}
BOOLEANS = frozenset({"event_day"})
FLAGS = frozenset(get_args(RiskFlag))
FIELDS = frozenset(get_args(DslField))
OPS = frozenset(get_args(DslOp))
MAX_FILTERS = 4
MAX_IN = 10
QUERY_KEYS = frozenset({"filters", "compare_to", "metric"})
FILTER_KEYS = frozenset({"field", "op", "value"})
DEMOTION_MONTH = "lessons_demotion_month"  # meta: the month whose demotion check has run
_COMPARE = {"gt": operator.gt, "gte": operator.ge, "lt": operator.lt, "lte": operator.le}

# Every whitelisted field has exactly one type: a field added to the schema fails here first.
_TYPED = NUMBERS | set(CATEGORIES) | BOOLEANS | {"risk_flags"}
assert _TYPED == FIELDS, sorted(_TYPED ^ FIELDS)


class QueryError(ValueError):
    """The query breaks S§6.2's rules; the message says how."""


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    value: float | str | bool | tuple[str, ...]

    def holds(self, value: Any) -> bool:
        """Does a known value meet the condition? (Unknown values never reach here.)"""
        op, want = self.op, self.value
        if op == "contains":
            return bool(want in value)
        if op == "in":
            return isinstance(want, tuple) and value in want
        if op == "eq":
            return bool(value == want)
        if op == "ne":
            return bool(value != want)
        bound = float(want) if isinstance(want, int | float) else math.nan
        return bool(_COMPARE[op](float(value), bound))


def validate(raw: str | Mapping[str, Any]) -> tuple[Condition, ...]:
    """The query's conditions, or ``QueryError`` naming the first thing wrong."""
    try:
        query = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raise QueryError("the query isn't JSON") from None
    if not isinstance(query, Mapping) or set(query) != QUERY_KEYS:
        raise QueryError(f"a query has exactly {sorted(QUERY_KEYS)}")
    if query["compare_to"] != "complement" or query["metric"] != "shadow_r":
        raise QueryError("a query compares with the complement, on shadow_r")
    filters = query["filters"]
    if not isinstance(filters, list) or not 1 <= len(filters) <= MAX_FILTERS:
        raise QueryError(f"a query has 1 to {MAX_FILTERS} filters")
    return tuple(_condition(f) for f in filters)


def _condition(f: Any) -> Condition:
    if not isinstance(f, Mapping) or set(f) != FILTER_KEYS:
        raise QueryError(f"a filter has exactly {sorted(FILTER_KEYS)}")
    field, op, value = f["field"], f["op"], f["value"]
    if not isinstance(field, str) or field not in FIELDS:
        raise QueryError(f"unknown field {field!r}")
    if not isinstance(op, str) or op not in OPS:
        raise QueryError(f"unknown op {op!r}")
    if field in NUMBERS:
        if op not in ("eq", "ne", "gt", "gte", "lt", "lte") or not _finite(value):
            raise QueryError(f"{field} takes eq, ne, gt, gte, lt or lte with a number")
        return Condition(field, op, float(value))
    if field in BOOLEANS:
        if op not in ("eq", "ne") or not isinstance(value, bool):
            raise QueryError(f"{field} takes eq or ne with true or false")
        return Condition(field, op, value)
    if field == "risk_flags":
        if op != "contains" or value not in FLAGS:
            raise QueryError("risk_flags takes contains with one risk flag")
        return Condition(field, op, value)
    allowed = CATEGORIES[field]
    if op in ("eq", "ne") and isinstance(value, str) and value in allowed:
        return Condition(field, op, value)
    if (
        op == "in"
        and isinstance(value, list)
        and 1 <= len(value) <= MAX_IN
        and all(isinstance(v, str) and v in allowed for v in value)
    ):
        return Condition(field, op, tuple(value))
    raise QueryError(f"{field} takes eq or ne with one of its values, or in with 1 to {MAX_IN}")


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


# ---------------------------------------------------------------- the rows


@dataclass(frozen=True)
class FeatureRow:
    day: date
    signal_id: str
    shadow_r: float
    values: Mapping[str, Any]  # every S§6.2 field; None when the engine didn't have it


def read_features(conn: sqlite3.Connection, epoch: int, *, last: date) -> list[FeatureRow]:
    """The epoch's triggered longs with a shadow R, through ``last``."""
    rows = conn.execute(
        "SELECT s.id, s.date, s.symbol, s.gap_pct, s.rel_vol, s.rel_vol_rank, s.trigger_price, "
        "s.armed_at, s.features_json, o.shadow_r, o.trigger_ts FROM signals s "
        "JOIN shadow_outcomes o ON o.signal_id = s.id WHERE s.eval_epoch = ? "
        "AND s.direction = 'long' AND s.date <= ? AND o.shadow_r IS NOT NULL ORDER BY s.date, s.id",
        (epoch, last.isoformat()),
    ).fetchall()
    if not rows:
        return []
    standing = standing_verdicts(conn, first=date.fromisoformat(rows[0]["date"]), last=last)
    out = []
    for r in rows:
        feats = json.loads(r["features_json"] or "{}")
        v = standing.get((r["date"], r["symbol"]))
        values = {
            "catalyst_type": v["catalyst_type"] if v else None,
            "risk_flags": tuple(json.loads(v["risk_flags_json"] or "[]")) if v else None,
            "verdict": v["verdict"] if v else None,
            "verdict_source": v["source"] if v else None,
            "confidence": _number(v["confidence"]) if v else None,
            "gap_pct": _number(r["gap_pct"]),
            "rel_vol": _number(r["rel_vol"]),
            "rel_vol_rank": _number(r["rel_vol_rank"]),
            "or_range_atr": _number(feats.get("or_range_atr")),
            "or_return_pct": _number(feats.get("or_return_pct")),
            "price": _number(float(r["trigger_price"])) if r["trigger_price"] else None,
            "atr_pct": _number(feats.get("atr_pct")),
            "minutes_to_trigger": _minutes(r["armed_at"], r["trigger_ts"]),
            "regime_spy_trend": feats.get("regime_spy_trend"),
            "regime_vol_bucket": feats.get("regime_vol_bucket"),
            "day_of_week": feats.get("day_of_week"),
            "event_day": _flag(feats.get("event_day")),
        }
        out.append(FeatureRow(date.fromisoformat(r["date"]), r["id"], float(r["shadow_r"]), values))
    return out


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _minutes(armed_at: str | None, trigger_ts: str | None) -> float | None:
    if not armed_at or not trigger_ts:
        return None
    elapsed = (parse_et_iso(trigger_ts) - parse_et_iso(armed_at)).total_seconds()
    return float(max(0, math.floor(elapsed / 60)))


# ---------------------------------------------------------------- the test


@dataclass(frozen=True)
class Evaluation:
    n_subset: int
    n_complement: int
    effect_r: float | None
    interval: Interval | None


def evaluate(
    conditions: Sequence[Condition], rows: Sequence[FeatureRow], boot: Bootstrap
) -> Evaluation:
    named = {c.field for c in conditions}
    by_day: dict[date, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
    for row in rows:
        if any(row.values.get(f) is None for f in named):
            continue  # an unknown field: neither side
        side = 0 if all(c.holds(row.values[c.field]) for c in conditions) else 1
        by_day[row.day][side].append(row.shadow_r)
    days = [by_day[d] for d in sorted(by_day)]
    subset = [r for s, _ in days for r in s]
    complement = [r for _, c in days for r in c]
    effect = (
        sum(subset) / len(subset) - sum(complement) / len(complement)
        if subset and complement
        else None
    )
    return Evaluation(
        n_subset=len(subset),
        n_complement=len(complement),
        effect_r=effect,
        interval=difference_interval(days, boot) if subset and complement else None,
    )


@dataclass(frozen=True)
class Lesson:
    id: str
    status: str
    statement: str
    query_json: str
    claimed_direction: str
    since: date | None  # the proposal's day: only later rows count


@dataclass(frozen=True)
class Outcome:
    lesson: Lesson
    status: Status
    reason: str
    evaluation: Evaluation | None


def judge(
    lesson: Lesson,
    rows: Sequence[FeatureRow],
    cfg: LessonsConfig,
    *,
    monthly: bool,
) -> Outcome:
    """What one evaluation makes of a proposed or promoted lesson (the module docstring)."""
    current: Status = "promoted" if lesson.status == "promoted" else "proposed"
    try:
        conditions = validate(lesson.query_json)
    except QueryError as exc:
        return Outcome(lesson, "rejected", f"invalid query: {exc}", None)
    later = [r for r in rows if lesson.since is None or r.day > lesson.since]
    ev = evaluate(conditions, later, Bootstrap.of(cfg))
    if current == "promoted":
        if monthly and not _passes(ev, lesson.claimed_direction, cfg, need_effect=False):
            return Outcome(lesson, "demoted", "the monthly re-test's interval reaches 0", ev)
        return Outcome(lesson, "promoted", "promoted", ev)
    need = cfg.min_n_per_side
    if ev.n_subset < need or ev.n_complement < need:
        return Outcome(
            lesson, "proposed", f"waiting: {ev.n_subset} and {ev.n_complement} of {need}", ev
        )
    if _passes(ev, lesson.claimed_direction, cfg, need_effect=True):
        return Outcome(lesson, "promoted", "passed its one test", ev)
    return Outcome(lesson, "rejected", "failed its one test", ev)


def _passes(ev: Evaluation, claimed: str, cfg: LessonsConfig, *, need_effect: bool) -> bool:
    i = ev.interval
    if ev.effect_r is None or i is None:
        return False
    if need_effect and abs(ev.effect_r) < cfg.min_effect_r:
        return False
    return i.high < 0 if claimed == "subset_worse" else i.low > 0


# ---------------------------------------------------------------- the journal


def read_lessons(conn: sqlite3.Connection, *, statuses: Sequence[str] = ()) -> list[Lesson]:
    wanted = tuple(statuses)
    where = f" WHERE status IN ({', '.join('?' * len(wanted))})" if wanted else ""
    return [
        Lesson(
            id=r["id"],
            status=r["status"],
            statement=r["statement"] or "",
            query_json=r["evidence_query_json"] or "",
            claimed_direction=r["claimed_direction"] or "",
            since=_since(r["source_date"], r["created_at"]),
        )
        for r in conn.execute(f"SELECT * FROM lessons{where} ORDER BY id", wanted)
    ]


def _since(source_date: str | None, created_at: str | None) -> date | None:
    if source_date:
        return date.fromisoformat(source_date)
    return parse_et_iso(created_at).date() if created_at else None


def evaluate_lessons(
    conn: sqlite3.Connection, *, now: datetime, cfg: LessonsConfig, write: bool
) -> list[Outcome]:
    """Judge every proposed and promoted lesson on the current epoch's rows through today. With
    ``write``, store each outcome, and that this month's demotion check has run."""
    month = now.strftime("%Y-%m")
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (DEMOTION_MONTH,)).fetchone()
    monthly = row is None or row[0] != month
    rows = read_features(conn, current_epoch(conn), last=now.date())
    outcomes = [
        judge(lesson, rows, cfg, monthly=monthly)
        for lesson in read_lessons(conn, statuses=("proposed", "promoted"))
    ]
    if write:
        for o in outcomes:
            _store(conn, o, now)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (DEMOTION_MONTH, month)
        )
    return outcomes


def _store(conn: sqlite3.Connection, o: Outcome, now: datetime) -> None:
    ev = o.evaluation
    at = fmt_iso(now)
    conn.execute(
        "UPDATE lessons SET status = ?, status_reason = ?, n_subset = ?, n_complement = ?, "
        "effect_r = ?, ci_low = ?, ci_high = ?, evaluated_at = ?, "
        "promoted_at = CASE WHEN ? = 'promoted' AND status != 'promoted' THEN ? "
        "ELSE promoted_at END, "
        "demoted_at = CASE WHEN ? = 'demoted' THEN ? ELSE demoted_at END WHERE id = ?",
        (o.status, o.reason,
         None if ev is None else ev.n_subset, None if ev is None else ev.n_complement,
         None if ev is None else ev.effect_r,
         None if ev is None or ev.interval is None else ev.interval.low,
         None if ev is None or ev.interval is None else ev.interval.high,
         at, o.status, at, o.status, at, o.lesson.id),
    )  # fmt: skip
