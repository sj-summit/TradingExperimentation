"""The backtest report (P3.9; DECISIONS D-042): Markdown plus one Parquet row per signal."""

import itertools
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from .engine import BacktestResult, SignalResult, TradeResult

RANK_BUCKETS = ((1, 5), (6, 10), (11, 15), (16, 20))
TRADING_DAYS = 252


@dataclass(frozen=True)
class TradeStats:
    n: int
    win_rate: float | None
    mean_r: float | None
    expectancy_usd: float | None  # mean net P&L per trade
    profit_factor: float | None
    net_pnl: float


@dataclass(frozen=True)
class EquityStats:
    start: float
    end: float
    total_return: float
    max_drawdown: float  # a positive fraction of the peak
    sharpe: float | None  # daily, annualized


@dataclass(frozen=True)
class ShadowStats:
    n: int
    win_rate: float | None
    mean_r: float | None


def trade_stats(trades: Sequence[TradeResult]) -> TradeStats:
    if not trades:
        return TradeStats(0, None, None, None, None, 0.0)
    net = [float(t.net_pnl) for t in trades]
    gains, losses = sum(x for x in net if x > 0), -sum(x for x in net if x < 0)
    return TradeStats(
        n=len(trades),
        win_rate=sum(x > 0 for x in net) / len(net),
        mean_r=statistics.fmean(t.r for t in trades),
        expectancy_usd=statistics.fmean(net),
        profit_factor=gains / losses if losses else None,
        net_pnl=sum(net),
    )


def equity_stats(result: BacktestResult) -> EquityStats:
    start = float(result.days[0].start_equity)
    curve = [start] + [float(d.end_cash) for d in result.days]
    peak, worst = curve[0], 0.0
    for value in curve:
        peak = max(peak, value)
        worst = max(worst, (peak - value) / peak if peak else 0.0)
    returns = [b / a - 1 for a, b in itertools.pairwise(curve) if a]
    sharpe = None
    if len(returns) > 1 and statistics.stdev(returns) > 0:
        sharpe = statistics.fmean(returns) / statistics.stdev(returns) * math.sqrt(TRADING_DAYS)
    return EquityStats(start=start, end=curve[-1], total_return=curve[-1] / start - 1,
                       max_drawdown=worst, sharpe=sharpe)  # fmt: skip


def shadow_stats(results: Sequence[SignalResult]) -> ShadowStats:
    rs = [r.shadow.shadow_r for r in results if r.shadow and r.shadow.shadow_r is not None]
    if not rs:
        return ShadowStats(0, None, None)
    return ShadowStats(n=len(rs), win_rate=sum(x > 0 for x in rs) / len(rs),
                       mean_r=statistics.fmean(rs))  # fmt: skip


def rank_buckets(results: Sequence[SignalResult]) -> list[tuple[str, ShadowStats]]:
    """H4: mean shadow R per rel_vol rank bucket (rank 1 = highest relative volume)."""
    return [
        (f"{lo}-{hi}", shadow_stats([r for r in results if lo <= r.signal.rel_vol_rank <= hi]))
        for lo, hi in RANK_BUCKETS
    ]


def h4_verdict(buckets: Sequence[tuple[str, ShadowStats]]) -> str:
    means = [s.mean_r for _, s in buckets if s.mean_r is not None]
    if len(means) < len(RANK_BUCKETS):
        return "insufficient data (a bucket has no triggered signals)"
    top, bottom = means[0], means[-1]
    falling = all(a >= b for a, b in itertools.pairwise(means))
    if top > bottom:
        shape = "falls monotonically" if falling else "is higher at the top but not monotone"
        return f"consistent with H4: mean shadow R {shape} from ranks 1-5 to 16-20"
    return "not consistent with H4: ranks 16-20 earn at least as much as ranks 1-5"


def render(result: BacktestResult) -> str:
    cfg = result.settings
    signals = [r for d in result.days for r in d.signals]
    longs = [r for r in signals if r.signal.direction == "long"]
    shorts = [r for r in signals if r.signal.direction == "shadow_short"]
    trades = [t for d in result.days for t in d.trades]
    ts, eq = trade_stats(trades), equity_stats(result)
    buckets_long, buckets_all = rank_buckets(longs), rank_buckets(longs + shorts)
    lines = [
        f"# Backtest {cfg.start} to {cfg.end} ({cfg.feed} feed)",
        "",
        "Rules-only ORB (STRATEGY.md §3) on historical data; no LLM (D-010). DECISIONS D-042 "
        "describes the method.",
        "",
        "## Coverage",
        "",
        f"- sessions: {len(result.days)}; signals: {len(signals)} "
        f"({len(longs)} long, {len(shorts)} shadow short); "
        f"triggered: {sum(d.triggered for d in result.days)}",
        f"- mean eligible symbols per day: {statistics.fmean(d.eligible for d in result.days):.0f}",
        f"- data requests this run: {result.requests} (a cached rerun makes none)",
        "",
        "## Book A long-only leg",
        "",
        f"- trades: {ts.n}; win rate: {_pct(ts.win_rate)}; mean R (gross): {_num(ts.mean_r)}",
        f"- expectancy per trade (net of fee estimates): {_usd(ts.expectancy_usd)}; "
        f"profit factor: {_num(ts.profit_factor)}; net P&L: {_usd(ts.net_pnl)}",
        f"- equity {_usd(eq.start)} -> {_usd(eq.end)} ({_pct(eq.total_return)}); "
        f"max drawdown {_pct(eq.max_drawdown)}; daily Sharpe {_num(eq.sharpe)}",
        f"- SPY buy-and-hold over the same days: {_pct(result.spy_return)}",
        "- triggered longs not traded: "
        + (", ".join(f"{k} {v}" for k, v in result.skips.items()) or "none"),
        "",
        "## Shadow outcomes (every triggered signal, capacity and cash ignored)",
        "",
        "| set | n | win rate | mean R |",
        "|---|---|---|---|",
    ]
    for name, subset in (("long", longs), ("shadow short", shorts), ("long + short", signals)):
        s = shadow_stats(subset)
        lines.append(f"| {name} | {s.n} | {_pct(s.win_rate)} | {_num(s.mean_r)} |")
    lines += ["", "## H4: mean shadow R by rel_vol rank", ""]
    lines += ["| ranks | long n | long mean R | all n | all mean R |", "|---|---|---|---|---|"]
    for (label, sl), (_, sa) in zip(buckets_long, buckets_all, strict=True):
        lines.append(f"| {label} | {sl.n} | {_num(sl.mean_r)} | {sa.n} | {_num(sa.mean_r)} |")
    lines += [
        "",
        f"- longs: {h4_verdict(buckets_long)}",
        f"- long + short: {h4_verdict(buckets_all)}",
        "",
        "## Limitations",
        "",
        "- Fills are simulated from SIP 1-minute bars: no queue position, impact or latency.",
        "- Triggers use SIP minute highs; live triggers use the IEX feed and lag the tape.",
        "- Inactive assets are included only where Alpaca still lists them; survivorship is "
        "reduced, not removed.",
        "- No event blackouts (no historical event calendar) and no halts.",
        "- A backtest is not a forecast; day-trading edges decay.",
        "",
    ]
    return "\n".join(lines)


def signal_rows(result: BacktestResult) -> list[dict[str, Any]]:
    rows = []
    for day in result.days:
        for r in day.signals:
            s, o, t = r.signal, r.shadow, r.trade
            rows.append(
                {
                    "date": s.date.isoformat(),
                    "symbol": s.symbol,
                    "rank": s.rel_vol_rank,
                    "rel_vol": s.rel_vol,
                    "direction": s.direction,
                    "gap_pct": s.gap_pct,
                    "or_volume": s.or_volume,
                    "trigger": _s(s.trigger_price),
                    "stop": _s(s.stop_price),
                    "triggered": bool(o and o.triggered),
                    "trigger_ts": o.trigger_ts.isoformat() if o and o.trigger_ts else None,
                    "exit_reason": o.exit_reason if o else None,
                    "shadow_r": o.shadow_r if o else None,
                    "mfe_r": o.mfe_r if o else None,
                    "mae_r": o.mae_r if o else None,
                    "minutes_held": o.minutes_held if o else None,
                    "traded": t is not None,
                    "skip": r.skip,
                    "qty": t.qty if t else None,
                    "entry": _s(t.entry_price) if t else None,
                    "exit": _s(t.exit_price) if t else None,
                    "net_pnl": _s(t.net_pnl) if t else None,
                    "trade_r": t.r if t else None,
                }
            )
    return rows


def write(result: BacktestResult, reports_dir: Path, data_dir: Path) -> tuple[Path, Path]:
    cfg = result.settings
    name = f"backtest-{cfg.start}-{cfg.end}-{cfg.feed}"
    reports_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    md = reports_dir / f"{name}.md"
    md.write_text(render(result), encoding="utf-8")
    table = data_dir / f"{name}.parquet"
    pq.write_table(pa.Table.from_pylist(signal_rows(result)), table)
    return md, table


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"
