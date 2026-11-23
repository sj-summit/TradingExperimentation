"""Measurement (STRATEGY.md §7.3; the Phase 5 plan's P5.1; DECISIONS D-075).

``read_epoch``, one read of the journal, gives an epoch's trades, the books' days, Book B's
standing verdicts on the triggered longs, the sessions and SPY's closes. Pure functions turn
them into:
- **each book's metrics** (``book_metrics``):
  - trades, win rate (R > 0), mean R, profit factor;
  - expectancy in dollars, gross and net of the estimated fees;
  - max drawdown and the daily Sharpe, from the book's days;
  - exposure, the share of session time it held a position;
  - mean entry slippage and delay;
  - live R against the same signal's shadow R, which measures how far the shadow's fills are
    from the broker's (D-072's stop-fill bias);
- **the veto value** (``veto_value``): V = mean shadow R of the triggered longs Book B allowed,
  minus that of those it vetoed, with each side's n and the vetoed side's own mean (D-075's
  population, below);
- **Book B minus Book A** (``b_minus_a``): their mean R per trade;
- **SPY buy-and-hold** (``spy_buy_and_hold``) over the books' sessions.

**Intervals** come from ``day_bootstrap``:
- it draws whole trading days with replacement (the human's decision, 2026-11-21: one day's
  signals share the market), ``lessons.bootstrap_resamples`` times from
  ``lessons.bootstrap_seed``;
- it gives percentile intervals at ``lessons.ci_level``;
- a resample where the statistic is undefined (a side with no signal) is skipped and counted.

**V's population:** the epoch's longs whose shadow triggered (on SIP bars, so a SIP-only trigger
counts), with a shadow R and a standing verdict (``verdict_view``).
- Allowed means ``allow`` or ``neutral``: neutral counts as allow (S§5). Vetoed means ``veto``.
- Triggered longs with no verdict (Book B off, a call that failed) are counted and left out.
"""

import math
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import numpy as np

from trader.config import LessonsConfig
from trader.core.clock import parse_et_iso
from trader.memory.journal import db_to_dec
from trader.memory.verdict_view import standing_verdicts

TRADING_DAYS_PER_YEAR = 252
ALLOWING = frozenset({"allow", "neutral"})  # neutral counts as allow (S§5)
EARLIEST = date(1970, 1, 1)

Stat = Callable[[np.ndarray], np.ndarray]


@dataclass(frozen=True)
class Bootstrap:
    """How intervals are drawn: from ``LessonsConfig`` (``lessons:`` in settings)."""

    resamples: int
    seed: int
    level: float

    @classmethod
    def of(cls, lessons: LessonsConfig) -> "Bootstrap":
        return cls(lessons.bootstrap_resamples, lessons.bootstrap_seed, lessons.ci_level)


@dataclass(frozen=True)
class Interval:
    low: float
    high: float
    used: int  # resamples the statistic was defined on
    skipped: int  # resamples with a side empty

    def excludes_zero(self) -> bool:
        return self.low > 0 or self.high < 0


# ---------------------------------------------------------------- the bootstrap


def day_bootstrap(days: np.ndarray, stat: Stat, boot: Bootstrap) -> Interval | None:
    """A percentile interval of ``stat`` over resampled days.

    ``days`` has one row per trading day, of that day's sufficient statistics (sums and counts);
    a resample draws as many days as there are, with replacement, and adds their rows up. ``stat``
    maps each resample's totals (one row each) to its value, NaN or infinite where undefined.
    None when there are no days, or the statistic is never defined."""
    if len(days) == 0:
        return None
    rng = np.random.default_rng(boot.seed)
    totals = np.zeros((boot.resamples, days.shape[1]))
    for _ in range(len(days)):  # a draw at a time: memory stays resamples x statistics
        totals += days[rng.integers(0, len(days), size=boot.resamples)]
    with np.errstate(divide="ignore", invalid="ignore"):
        values = stat(totals)
    defined = values[np.isfinite(values)]
    if defined.size == 0:
        return None
    tail = (1 - boot.level) / 2 * 100
    low, high = np.percentile(defined, [tail, 100 - tail])
    return Interval(float(low), float(high), int(defined.size), int(boot.resamples - defined.size))


def _sums(values: Iterable[float]) -> tuple[float, int]:
    listed = list(values)
    return float(sum(listed)), len(listed)


def mean_interval(by_day: Sequence[Sequence[float]], boot: Bootstrap) -> Interval | None:
    """The interval of a mean, its values grouped by day."""
    rows = np.array([_sums(day) for day in by_day if day], dtype=float).reshape(-1, 2)
    return day_bootstrap(rows, lambda t: t[:, 0] / t[:, 1], boot)


def difference_interval(
    by_day: Sequence[tuple[Sequence[float], Sequence[float]]], boot: Bootstrap
) -> Interval | None:
    """The interval of mean(first side) - mean(second side), both grouped by day."""
    rows = np.array([(*_sums(a), *_sums(b)) for a, b in by_day if a or b], dtype=float).reshape(
        -1, 4
    )
    return day_bootstrap(rows, lambda t: t[:, 0] / t[:, 1] - t[:, 2] / t[:, 3], boot)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


# ---------------------------------------------------------------- reading the journal


@dataclass(frozen=True)
class TradeRec:
    book: str
    day: date
    signal_id: str
    r: float | None
    pnl: Decimal | None  # gross: the fees are apart
    fees: Decimal
    entry_slippage: Decimal | None
    entry_delay_ms: int | None
    entry_ts: datetime
    exit_ts: datetime | None
    shadow_r: float | None  # the same signal's


@dataclass(frozen=True)
class DayRec:
    day: date
    book: str
    start_equity: Decimal | None
    end_equity: Decimal | None
    pnl: Decimal | None  # net of the day's estimated fees


@dataclass(frozen=True)
class LongRec:
    """A long whose shadow triggered, with Book B's standing verdict on it."""

    day: date
    signal_id: str
    shadow_r: float
    verdict: str | None


@dataclass(frozen=True)
class EpochData:
    epoch: int
    trades: tuple[TradeRec, ...]
    days: tuple[DayRec, ...]
    longs: tuple[LongRec, ...]
    sessions: Mapping[date, tuple[datetime, datetime]]  # open, close
    spy: Mapping[date, Decimal]  # session -> SPY's close


def read_epoch(
    conn: sqlite3.Connection, epoch: int, *, last: date, first: date | None = None
) -> EpochData:
    """The epoch's records from ``first`` (default: its start) through ``last``."""
    span = ((first or EARLIEST).isoformat(), last.isoformat())
    trades = tuple(
        TradeRec(
            book=r["book"],
            day=date.fromisoformat(r["entry_ts"][:10]),
            signal_id=r["signal_id"],
            r=r["r_multiple"],
            pnl=db_to_dec(r["pnl"]),
            fees=db_to_dec(r["fees_est"]) or Decimal(0),
            entry_slippage=db_to_dec(r["entry_slippage"]),
            entry_delay_ms=r["entry_delay_ms"],
            entry_ts=parse_et_iso(r["entry_ts"]),
            exit_ts=parse_et_iso(r["exit_ts"]) if r["exit_ts"] else None,
            shadow_r=r["shadow_r"],
        )
        for r in conn.execute(
            "SELECT t.*, o.shadow_r FROM trades t LEFT JOIN shadow_outcomes o "
            "ON o.signal_id = t.signal_id WHERE t.eval_epoch = ? "
            "AND substr(t.entry_ts, 1, 10) BETWEEN ? AND ? ORDER BY t.entry_ts, t.id",
            (epoch, *span),
        )
    )
    days = tuple(
        DayRec(
            day=date.fromisoformat(r["date"]),
            book=r["book"],
            start_equity=db_to_dec(r["start_equity"]),
            end_equity=db_to_dec(r["end_equity"]),
            pnl=db_to_dec(r["pnl"]),
        )
        for r in conn.execute(
            "SELECT * FROM daily_stats WHERE eval_epoch = ? AND date BETWEEN ? AND ? "
            "ORDER BY date, book",
            (epoch, *span),
        )
    )
    shadows = conn.execute(
        "SELECT s.id, s.date, s.symbol, o.shadow_r FROM signals s JOIN shadow_outcomes o "
        "ON o.signal_id = s.id WHERE s.eval_epoch = ? AND s.direction = 'long' "
        "AND s.date BETWEEN ? AND ? AND o.shadow_r IS NOT NULL ORDER BY s.date, s.id",
        (epoch, *span),
    ).fetchall()
    standing = (
        standing_verdicts(
            conn,
            first=min(date.fromisoformat(r["date"]) for r in shadows),
            last=last,
        )
        if shadows
        else {}
    )
    longs = tuple(
        LongRec(
            day=date.fromisoformat(r["date"]),
            signal_id=r["id"],
            shadow_r=float(r["shadow_r"]),
            verdict=v["verdict"] if (v := standing.get((r["date"], r["symbol"]))) else None,
        )
        for r in shadows
    )
    sessions = {
        date.fromisoformat(r["date"]): (parse_et_iso(r["open_ts"]), parse_et_iso(r["close_ts"]))
        for r in conn.execute(
            "SELECT date, open_ts, close_ts FROM trading_days WHERE date BETWEEN ? AND ? "
            "AND open_ts IS NOT NULL AND close_ts IS NOT NULL",
            span,
        )
    }
    spy = {
        date.fromisoformat(r["date"]): Decimal(r["spy_close"])
        for r in conn.execute(
            "SELECT date, spy_close FROM regime_daily WHERE date <= ? AND spy_close IS NOT NULL "
            "ORDER BY date",
            (last.isoformat(),),
        )
    }
    return EpochData(epoch, trades, days, longs, sessions, spy)


# ---------------------------------------------------------------- each book


@dataclass(frozen=True)
class BookMetrics:
    book: str
    days: int
    trades: int
    win_rate: float | None
    mean_r: float | None
    expectancy_gross_usd: float | None
    expectancy_net_usd: float | None
    profit_factor: float | None  # net: gains over losses; None without a loss
    net_pnl_usd: float | None  # the days' P&L, after the estimated fees
    max_drawdown_pct: float | None
    daily_sharpe: float | None  # annualized: x sqrt(252)
    exposure: float | None  # share of session time holding a position
    mean_entry_slippage: float | None
    mean_entry_delay_ms: float | None
    live_minus_shadow_r: float | None  # mean, over trades whose signal has a shadow R
    live_vs_shadow_n: int


def book_metrics(
    book: str,
    trades: Sequence[TradeRec],
    days: Sequence[DayRec],
    sessions: Mapping[date, tuple[datetime, datetime]],
) -> BookMetrics:
    mine = [t for t in trades if t.book == book]
    book_days = sorted((d for d in days if d.book == book), key=lambda d: d.day)
    rs = [t.r for t in mine if t.r is not None]
    closed = [t for t in mine if t.pnl is not None]
    gross = [float(t.pnl) for t in closed if t.pnl is not None]
    net = [float(t.pnl - t.fees) for t in closed if t.pnl is not None]
    gains, losses = sum(x for x in net if x > 0), -sum(x for x in net if x < 0)
    pairs = [t.r - t.shadow_r for t in mine if t.r is not None and t.shadow_r is not None]
    pnls = [float(d.pnl) for d in book_days if d.pnl is not None]
    return BookMetrics(
        book=book,
        days=len(book_days),
        trades=len(mine),
        win_rate=sum(r > 0 for r in rs) / len(rs) if rs else None,
        mean_r=_mean(rs),
        expectancy_gross_usd=_mean(gross),
        expectancy_net_usd=_mean(net),
        profit_factor=gains / losses if losses > 0 else None,
        net_pnl_usd=sum(pnls) if pnls else None,
        max_drawdown_pct=max_drawdown_pct(book_days),
        daily_sharpe=daily_sharpe(book_days),
        exposure=exposure(mine, [d.day for d in book_days], sessions),
        mean_entry_slippage=_mean(
            [float(t.entry_slippage) for t in mine if t.entry_slippage is not None]
        ),
        mean_entry_delay_ms=_mean(
            [float(t.entry_delay_ms) for t in mine if t.entry_delay_ms is not None]
        ),
        live_minus_shadow_r=_mean(pairs),
        live_vs_shadow_n=len(pairs),
    )


def max_drawdown_pct(days: Sequence[DayRec]) -> float | None:
    """The deepest fall from a peak, in percent of the peak, over the first day's start equity
    and each day's end equity."""
    path = [days[0].start_equity] if days else []
    path += [d.end_equity for d in days]
    worst, peak = None, None
    for equity in path:
        if equity is None:
            continue
        peak = equity if peak is None else max(peak, equity)
        if peak > 0:
            fall = float((peak - equity) / peak * 100)
            worst = fall if worst is None else max(worst, fall)
    return worst


def daily_sharpe(days: Sequence[DayRec]) -> float | None:
    """Mean daily return over its standard deviation, x sqrt(252); None under two days or with
    no variation."""
    returns = [
        float(d.pnl / d.start_equity)
        for d in days
        if d.pnl is not None and d.start_equity is not None and d.start_equity > 0
    ]
    if len(returns) < 2:
        return None
    sd = float(np.std(returns, ddof=1))
    return float(np.mean(returns)) / sd * math.sqrt(TRADING_DAYS_PER_YEAR) if sd > 0 else None


def exposure(
    trades: Sequence[TradeRec],
    days: Sequence[date],
    sessions: Mapping[date, tuple[datetime, datetime]],
) -> float | None:
    """The share of the days' session time with at least one position open: overlapping trades
    count once, and a trade with no exit holds to the close."""
    held = total = 0.0
    for day in days:
        if day not in sessions:
            continue
        opened, closed = sessions[day]
        total += (closed - opened).total_seconds()
        spans = sorted(
            (max(t.entry_ts, opened), min(t.exit_ts or closed, closed))
            for t in trades
            if t.day == day
        )
        end: datetime | None = None
        for start, stop in spans:
            if end is not None and start < end:
                start = end
            if stop > start:
                held += (stop - start).total_seconds()
                end = stop
    return held / total if total > 0 else None


# ---------------------------------------------------------------- Book B's vetoes


@dataclass(frozen=True)
class VetoValue:
    n_allowed: int
    n_vetoed: int
    n_no_verdict: int  # triggered longs left out: Book B gave no verdict
    mean_allowed: float | None
    mean_vetoed: float | None  # the vetoed side's own mean shadow R
    v: float | None
    v_interval: Interval | None
    vetoed_interval: Interval | None


def veto_value(longs: Sequence[LongRec], boot: Bootstrap) -> VetoValue:
    allowed = [x for x in longs if x.verdict in ALLOWING]
    vetoed = [x for x in longs if x.verdict == "veto"]
    by_day: dict[date, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
    for x in allowed:
        by_day[x.day][0].append(x.shadow_r)
    for x in vetoed:
        by_day[x.day][1].append(x.shadow_r)
    days = [by_day[d] for d in sorted(by_day)]
    mean_a = _mean([x.shadow_r for x in allowed])
    mean_v = _mean([x.shadow_r for x in vetoed])
    return VetoValue(
        n_allowed=len(allowed),
        n_vetoed=len(vetoed),
        n_no_verdict=sum(x.verdict is None for x in longs),
        mean_allowed=mean_a,
        mean_vetoed=mean_v,
        v=None if mean_a is None or mean_v is None else mean_a - mean_v,
        v_interval=difference_interval(days, boot) if allowed and vetoed else None,
        vetoed_interval=mean_interval([b for _, b in days], boot) if vetoed else None,
    )


# ---------------------------------------------------------------- the books compared


@dataclass(frozen=True)
class Difference:
    value: float | None
    interval: Interval | None
    n_first: int
    n_second: int


def b_minus_a(trades: Sequence[TradeRec], boot: Bootstrap) -> Difference:
    """Book B's mean R per trade minus Book A's (H2)."""
    by_day: dict[date, tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
    for t in trades:
        if t.r is not None and t.book in ("A", "B"):
            by_day[t.day][0 if t.book == "B" else 1].append(t.r)
    days = [by_day[d] for d in sorted(by_day)]
    b = [r for side, _ in days for r in side]
    a = [r for _, side in days for r in side]
    mean_b, mean_a = _mean(b), _mean(a)
    return Difference(
        value=None if mean_b is None or mean_a is None else mean_b - mean_a,
        interval=difference_interval(days, boot) if a and b else None,
        n_first=len(b),
        n_second=len(a),
    )


# ---------------------------------------------------------------- SPY


@dataclass(frozen=True)
class SpyHold:
    """SPY bought at the close before the books' first session and held to the last."""

    start: date | None  # the session whose close it was bought at
    end: date | None  # the session whose close it was valued at
    return_pct: float | None
    covered: bool  # the closes reach the books' last session


def spy_buy_and_hold(spy: Mapping[date, Decimal], days: Sequence[date]) -> SpyHold:
    if not days:
        return SpyHold(None, None, None, covered=False)
    first, last = min(days), max(days)
    before = [d for d in spy if d < first]
    through = [d for d in spy if d <= last]
    if not before or not through or max(through) <= max(before):
        return SpyHold(None, None, None, covered=False)
    start, end = max(before), max(through)
    change = float((spy[end] - spy[start]) / spy[start] * 100)
    return SpyHold(start, end, change, covered=end == last)


def spy_path(spy: Mapping[date, Decimal], days: Sequence[date]) -> dict[date, float]:
    """SPY's close on each of ``days`` it has, indexed to 100 at the close before the first."""
    hold = spy_buy_and_hold(spy, days)
    if hold.start is None:
        return {}
    base = spy[hold.start]
    return {d: float(spy[d] / base * 100) for d in sorted(days) if d in spy}


# ---------------------------------------------------------------- the epoch


@dataclass(frozen=True)
class EpochSummary:
    epoch: int
    first: date | None
    last: date | None
    books: Mapping[str, BookMetrics]
    veto: VetoValue
    b_minus_a: Difference
    spy: SpyHold
    triggered_longs: int


def summarize(data: EpochData, boot: Bootstrap) -> EpochSummary:
    sessions = sorted({d.day for d in data.days})
    return EpochSummary(
        epoch=data.epoch,
        first=sessions[0] if sessions else None,
        last=sessions[-1] if sessions else None,
        books={
            book: book_metrics(book, data.trades, data.days, data.sessions)
            for book in sorted({d.book for d in data.days} | {t.book for t in data.trades})
        },
        veto=veto_value(data.longs, boot),
        b_minus_a=b_minus_a(data.trades, boot),
        spy=spy_buy_and_hold(data.spy, sessions),
        triggered_longs=len(data.longs),
    )
