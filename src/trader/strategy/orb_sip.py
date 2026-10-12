"""The 09:35 opening-range scan (S§3.1-3.2; DECISIONS D-040).

``select`` is the pure rule set, shared by the live scan and the backtest. ``OrbSipSelector``
fetches today's 5-minute opening-range bars for every eligible symbol, waits first until the
last opening minute has been published, re-checks late corporate actions, and persists the
top 20 to ``signals`` and every eligible symbol's outcome to ``or_scan.parquet``.

The scan never writes ``or_bars``: the next universe build fetches today's final bar, so the
relative-volume baselines always come from final bars.
"""

import json
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal, Protocol

import structlog
from pydantic import BaseModel, ConfigDict

from trader.config import StrategyConfig
from trader.core.calendar import TradingCalendar
from trader.core.clock import Clock, et_datetime, fmt_iso, parse_et_iso
from trader.core.ids import signal_id
from trader.core.models import Bar, FeatureValue, Signal, Split
from trader.data import indicators
from trader.data.alpaca_rest import Adjustment, AlpacaError, Feed, Timeframe
from trader.data.recorder import Recorder
from trader.memory.journal import Journal, db_to_dec, dec_to_db
from trader.strategy.pricing import long_prices, short_prices

log = structlog.get_logger(__name__)

SPLIT_LOOKAROUND = timedelta(days=7)  # Alpaca may list a split late; filter on ex_date (D-026)
# or_open / prev_close this close to a split ratio means a split Alpaca hasn't listed (D-026).
SPLIT_RATIOS = tuple(Decimal(1) / n for n in (2, 3, 4, 5, 10)) + tuple(
    Decimal(n) for n in (5, 10, 15, 20, 25, 30, 40, 50)
)
SUSPECT_TOLERANCE = Decimal("0.03")
READY_POLL = timedelta(seconds=0.5)
READY_GRACE = timedelta(seconds=8)  # stop waiting for the last opening minute at 09:35:08
SLOW_SCAN_S = 30.0  # a scan inside the deadline that took longer than this is a P3

# Why a scan armed nothing: it finished late, the engine started late, or the clock is off
# (RISK.md section 9 refuses to arm on clock skew; DECISIONS D-048).
ScanFlag = Literal["scan_late", "late_start", "clock_skew"]
Outcome = Literal[
    "selected", "split", "no_bar", "suspect_gap", "or_volume", "or_vol_avg", "price", "rel_vol",
    "rank",
]  # fmt: skip


class ScanError(ValueError):
    """The scan can't run: no eligible universe for the day."""


class ScanMarket(Protocol):
    """The slice of AlpacaRest the scan reads."""

    async def bars(
        self,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
        *,
        feed: Feed,
        adjustment: Adjustment = "raw",
    ) -> dict[str, list[Bar]]: ...

    async def splits(self, start: date, end: date) -> list[Split]: ...


@dataclass(frozen=True)
class Candidate:
    """An eligible symbol from ``universe_daily`` (S§3.1)."""

    symbol: str
    prev_close: Decimal
    atr14: Decimal
    adv14: int
    or_vol_avg14: float


class ScanRow(BaseModel):
    """One eligible symbol's opening range and what the scan made of it (``or_scan.parquet``)."""

    model_config = ConfigDict(frozen=True)

    date: date
    symbol: str
    prev_close: Decimal
    atr14: Decimal
    adv14: int
    or_vol_avg14: float
    or_open: Decimal | None
    or_high: Decimal | None
    or_low: Decimal | None
    or_close: Decimal | None
    or_volume: int
    rel_vol: float | None
    gap_pct: float | None
    outcome: Outcome
    rank: int | None  # among the survivors of every filter, whether or not inside the top N


@dataclass(frozen=True)
class ScanResult:
    day: date
    signals: tuple[Signal, ...]
    rows: tuple[ScanRow, ...] = ()  # empty when the signals were loaded from an earlier run
    started_at: datetime | None = None
    finished_at: datetime | None = None
    bar_wait_s: float = 0.0
    flag: ScanFlag | None = None
    resumed: bool = False

    @property
    def armed(self) -> list[Signal]:
        return [s for s in self.signals if s.status == "armed"]

    @property
    def counts(self) -> dict[str, int]:
        return dict(sorted(Counter(row.outcome for row in self.rows).items()))

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


# ---------------------------------------------------------------- the rules (pure)


def suspect_gap(or_open: Decimal, prev_close: Decimal) -> bool:
    """True when the open sits within 3% of a split ratio of the previous close: most likely a
    split Alpaca hasn't listed yet, which would distort ATR14 and the relative volume."""
    if prev_close <= 0:
        return False
    ratio = or_open / prev_close
    return any(abs(ratio - r) <= SUSPECT_TOLERANCE * r for r in SPLIT_RATIOS)


def select(
    day: date,
    candidates: Sequence[Candidate],
    bars: Mapping[str, Bar],
    *,
    strategy: StrategyConfig,
    feed: Feed,
    split_symbols: frozenset[str] = frozenset(),
) -> list[ScanRow]:
    """S§3.2 steps 1-5 for every eligible symbol: the first failing filter names the outcome,
    and the survivors are ranked by rel_vol (ties: higher OR volume, then symbol).

    Splits and suspect gaps are dropped before ranking, so they never take a top-N slot. The
    IEX liquidity guards are off on the SIP feed.
    """
    cfg = strategy.selection
    guards = feed == "iex"
    rows: list[ScanRow] = []
    survivors: list[tuple[float, int, str]] = []
    for c in sorted(candidates, key=lambda c: c.symbol):
        bar = bars.get(c.symbol)
        volume = bar.volume if bar is not None else 0
        rv = indicators.rel_vol(volume, c.or_vol_avg14) if bar is not None else None
        outcome: Outcome | None = None
        if c.symbol in split_symbols:
            outcome = "split"
        elif bar is None:
            outcome = "no_bar"
        elif suspect_gap(bar.open, c.prev_close):
            outcome = "suspect_gap"
        elif guards and volume < cfg.min_or_volume:
            outcome = "or_volume"
        elif guards and c.or_vol_avg14 < cfg.min_or_vol_avg:
            outcome = "or_vol_avg"
        elif not bar.open > cfg.min_open_price:
            outcome = "price"
        elif rv is None or rv < cfg.rel_vol_min:
            outcome = "rel_vol"
        else:
            survivors.append((rv, volume, c.symbol))
        rows.append(_row(day, c, bar, rv, outcome or "selected"))
    ranked = sorted(survivors, key=lambda s: (-s[0], -s[1], s[2]))
    ranks = {symbol: i for i, (_, _, symbol) in enumerate(ranked, start=1)}
    return [
        row.model_copy(
            update={
                "rank": ranks[row.symbol],
                "outcome": "selected" if ranks[row.symbol] <= cfg.top_n else "rank",
            }
        )
        if row.symbol in ranks
        else row
        for row in rows
    ]


def selected(rows: Sequence[ScanRow]) -> list[ScanRow]:
    """The top-N rows in rank order."""
    return sorted((r for r in rows if r.outcome == "selected"), key=lambda r: r.rank or 0)


def to_signal(
    row: ScanRow,
    *,
    strategy: StrategyConfig,
    max_chase_pct: Decimal,
    features: Mapping[str, FeatureValue] | None = None,
    flag: ScanFlag | None = None,
) -> Signal:
    """One top-N row as a signal (S§3.2 step 6; S§3.3-3.4 prices; S§7.2.5 for shorts).

    A flagged (late) scan arms nothing: every row is ``not_armed`` but still saved, so shadow
    outcomes and H4 keep the day.
    """
    if row.or_open is None or row.or_high is None or row.or_low is None or row.or_close is None:
        raise ValueError(f"{row.symbol}: a selected row must have an opening range")
    if row.rel_vol is None or row.gap_pct is None or row.rank is None:
        raise ValueError(f"{row.symbol}: a selected row must have rel_vol, gap and rank")
    direction: Literal["long", "shadow_short", "none"] = (
        "long"
        if row.or_close > row.or_open
        else "shadow_short"
        if row.or_close < row.or_open
        else "none"
    )
    trigger = stop = chase = limit = None
    if direction == "long":
        lp = long_prices(
            row.or_high,
            row.atr14,
            entry=strategy.entry,
            exit=strategy.exit,
            max_chase_pct=max_chase_pct,
        )
        trigger, stop, chase, limit = lp.trigger, lp.stop, lp.max_chase, lp.nominal_limit
    elif direction == "shadow_short" and strategy.selection.record_shadow_shorts:
        sp = short_prices(row.or_low, row.atr14, entry=strategy.entry, exit=strategy.exit)
        trigger, stop = sp.trigger, sp.stop
    feats: dict[str, FeatureValue] = {
        "or_range_atr": indicators.or_range_atr(row.or_high, row.or_low, row.atr14),
        "or_return_pct": indicators.or_return_pct(row.or_open, row.or_close),
        "gap_pct": row.gap_pct,
        "atr_pct": indicators.atr_pct(row.atr14, row.prev_close),
        **(features or {}),
    }
    if flag is not None:
        feats[flag] = True
    return Signal(
        id=signal_id(row.date, row.symbol),
        date=row.date,
        symbol=row.symbol,
        direction=direction,
        status="armed" if direction == "long" and flag is None else "not_armed",
        rel_vol=row.rel_vol,
        rel_vol_rank=row.rank,
        or_open=row.or_open,
        or_high=row.or_high,
        or_low=row.or_low,
        or_close=row.or_close,
        or_volume=row.or_volume,
        atr14=row.atr14,
        adv14=row.adv14,
        prev_close=row.prev_close,
        gap_pct=row.gap_pct,
        trigger_price=trigger,
        stop_price=stop,
        max_chase_price=chase,
        entry_limit=limit,
        features=feats,
    )


def _row(day: date, c: Candidate, bar: Bar | None, rv: float | None, outcome: Outcome) -> ScanRow:
    return ScanRow(
        date=day,
        symbol=c.symbol,
        prev_close=c.prev_close,
        atr14=c.atr14,
        adv14=c.adv14,
        or_vol_avg14=c.or_vol_avg14,
        or_open=bar.open if bar else None,
        or_high=bar.high if bar else None,
        or_low=bar.low if bar else None,
        or_close=bar.close if bar else None,
        or_volume=bar.volume if bar else 0,
        rel_vol=rv,
        gap_pct=indicators.gap_pct(bar.open, c.prev_close) if bar and c.prev_close > 0 else None,
        outcome=outcome,
        rank=None,
    )


# ---------------------------------------------------------------- the live scan


class OrbSipSelector:
    """Runs the scan for one session (A§4.3). Every wait runs on the injected clock."""

    def __init__(
        self,
        *,
        market: ScanMarket,
        journal: Journal,
        clock: Clock,
        calendar: TradingCalendar,
        strategy: StrategyConfig,
        max_chase_pct: Decimal,
        feed: Feed,
        ready_symbol: str,
        recorder: Recorder | None = None,
    ) -> None:
        self._market, self._journal, self._clock = market, journal, clock
        self._calendar, self._strategy = calendar, strategy
        self._max_chase_pct, self._feed, self._ready_symbol = max_chase_pct, feed, ready_symbol
        self._recorder = recorder

    async def scan(
        self,
        day: date,
        *,
        deadline: datetime | None = None,
        late: ScanFlag | None = None,
        features: Mapping[str, FeatureValue] | None = None,
        persist: bool = True,
    ) -> ScanResult:
        """Scan ``day``. Finishing after ``deadline`` (or being told ``late``) saves every row
        ``not_armed``. A persisted scan that already ran (a restart) is loaded, not refetched."""
        if persist:
            saved = await load_signals(self._journal, day)
            if saved:
                return ScanResult(day=day, signals=tuple(saved), resumed=True)
        started = self._clock.now()
        candidates = await read_candidates(self._journal, day)
        if not candidates:
            raise ScanError(
                f"no eligible universe for {day}: run `trader universe build --date {day}` first"
            )
        session = self._calendar.session(day)
        or_start = session.open
        or_end = or_start + timedelta(minutes=self._strategy.selection.or_minutes)
        waited = await self._await_last_minute(or_end) if self._feed == "iex" else 0.0
        symbols = [c.symbol for c in candidates]
        fetched = await self._market.bars(
            symbols,
            "5Min",
            or_start,
            or_end - timedelta(seconds=1),
            feed=self._feed,
            adjustment="split",
        )
        bars = {
            s: bar
            for s in symbols
            if (bar := next((b for b in fetched.get(s, []) if b.ts == or_start), None))
        }
        prev = self._calendar.previous_session(day)
        found = await self._market.splits(prev - SPLIT_LOOKAROUND, day + SPLIT_LOOKAROUND)
        split_symbols = frozenset(s.symbol for s in found if s.ex_date in (prev, day))
        rows = select(
            day,
            candidates,
            bars,
            strategy=self._strategy,
            feed=self._feed,
            split_symbols=split_symbols,
        )
        finished = self._clock.now()
        flag: ScanFlag | None = late or (
            "scan_late" if deadline is not None and finished > deadline else None
        )
        signals = tuple(
            to_signal(
                row,
                strategy=self._strategy,
                max_chase_pct=self._max_chase_pct,
                features=features,
                flag=flag,
            )
            for row in selected(rows)
        )
        result = ScanResult(
            day=day,
            signals=signals,
            rows=tuple(rows),
            started_at=started,
            finished_at=finished,
            bar_wait_s=waited,
            flag=flag,
        )
        if persist:
            await save_signals(self._journal, signals)
            if self._recorder is not None:
                self._recorder.write_or_scan(rows)
        log.info(
            "scan done",
            day=day.isoformat(),
            eligible=len(rows),
            selected=len(signals),
            armed=len(result.armed),
            flag=flag,
            bar_wait_s=round(waited, 2),
            duration_s=result.duration_s,
            outcomes=result.counts,
        )
        return result

    async def _await_last_minute(self, or_end: datetime) -> float:
        """Wait until the last opening minute's bar exists for the ready symbol (SPY), polling
        every 0.5 s up to 09:35:08, so the 5-minute bars include it. A past session's bars are
        final: no wait."""
        start = self._clock.now()
        if start >= or_end + timedelta(minutes=1):
            return 0.0
        last_minute = or_end - timedelta(minutes=1)
        give_up = or_end + READY_GRACE
        while True:
            try:
                got = await self._market.bars(
                    [self._ready_symbol],
                    "1Min",
                    last_minute,
                    or_end - timedelta(seconds=1),
                    feed=self._feed,
                )
                if any(b.ts == last_minute for b in got.get(self._ready_symbol, [])):
                    return (self._clock.now() - start).total_seconds()
            except AlpacaError as exc:
                log.warning("scan readiness check failed", error=str(exc))
            if self._clock.now() >= give_up:
                log.warning(
                    "last opening minute not published; scanning anyway",
                    symbol=self._ready_symbol,
                )
                return (self._clock.now() - start).total_seconds()
            await self._clock.sleep_until(min(self._clock.now() + READY_POLL, give_up))


# ---------------------------------------------------------------- `trader scan`


def last_scanned_session(calendar: TradingCalendar, now: datetime, scan_at: time) -> date:
    """Today once its scan time has passed, else the previous session."""
    today = now.date()
    if calendar.is_trading_day(today) and now >= et_datetime(today, scan_at):
        return today
    return calendar.previous_session(today)


def scan_lines(result: ScanResult, feed: Feed) -> list[str]:
    """A printable summary of a scan."""
    excluded = {k: v for k, v in result.counts.items() if k != "selected"}
    lines = [
        f"scan {result.day} ({feed}): {len(result.rows)} eligible, {len(result.signals)} selected, "
        f"{len(result.armed)} armed; last-minute wait {result.bar_wait_s:.1f} s",
        "excluded: " + (", ".join(f"{k} {v}" for k, v in excluded.items()) or "none"),
        f"{'rank':>4} {'symbol':<6} {'dir':<12} {'rel_vol':>7} {'open':>8} {'high':>8} "
        f"{'low':>8} {'close':>8} {'volume':>9} {'trigger':>8} {'stop':>8} {'gap%':>6}",
    ]
    for s in result.signals:
        lines.append(
            f"{s.rel_vol_rank:>4} {s.symbol:<6} {s.direction:<12} {s.rel_vol:>7.2f} "
            f"{s.or_open:>8} {s.or_high:>8} {s.or_low:>8} {s.or_close:>8} {s.or_volume:>9,} "
            f"{s.trigger_price or '-'!s:>8} {s.stop_price or '-'!s:>8} {s.gap_pct:>6.1f}"
        )
    return lines


# ---------------------------------------------------------------- journal


async def read_candidates(journal: Journal, day: date) -> list[Candidate]:
    rows = await journal.fetch_all(
        "SELECT symbol, prev_close, atr14, adv14, or_vol_avg14 FROM universe_daily "
        "WHERE date = ? AND eligible = 1 ORDER BY symbol",
        (day.isoformat(),),
    )
    return [
        Candidate(
            symbol=r["symbol"],
            prev_close=Decimal(r["prev_close"]),
            atr14=Decimal(r["atr14"]),
            adv14=int(r["adv14"]),
            or_vol_avg14=float(r["or_vol_avg14"]),
        )
        for r in rows
    ]


_SIGNAL_COLUMNS = (
    "id, date, symbol, strategy, direction, status, rel_vol, rel_vol_rank, or_open, or_high, "
    "or_low, or_close, or_volume, atr14, adv14, prev_close, gap_pct, trigger_price, stop_price, "
    "entry_limit, max_chase_price, armed_at, triggered_at, features_json"
)


def insert_signals(conn: sqlite3.Connection, signals: Sequence[Signal]) -> None:
    """Insert the scan's signals inside the caller's transaction, stamped with the epoch."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'eval_epoch'").fetchone()
    epoch = int(row[0]) if row is not None else 0
    conn.executemany(
        f"INSERT INTO signals ({_SIGNAL_COLUMNS}, eval_epoch) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                s.id,
                s.date.isoformat(),
                s.symbol,
                s.strategy,
                s.direction,
                s.status,
                s.rel_vol,
                s.rel_vol_rank,
                dec_to_db(s.or_open),
                dec_to_db(s.or_high),
                dec_to_db(s.or_low),
                dec_to_db(s.or_close),
                s.or_volume,
                dec_to_db(s.atr14),
                s.adv14,
                dec_to_db(s.prev_close),
                s.gap_pct,
                dec_to_db(s.trigger_price),
                dec_to_db(s.stop_price),
                dec_to_db(s.entry_limit),
                dec_to_db(s.max_chase_price),
                fmt_iso(s.armed_at) if s.armed_at else None,
                fmt_iso(s.triggered_at) if s.triggered_at else None,
                json.dumps(s.features, sort_keys=True),
                epoch,
            )
            for s in signals
        ],
    )


async def save_signals(journal: Journal, signals: Sequence[Signal]) -> None:
    await journal.write(lambda conn: insert_signals(conn, signals))


async def load_signals(journal: Journal, day: date) -> list[Signal]:
    """The day's signals in rank order."""
    rows = await journal.fetch_all(
        f"SELECT {_SIGNAL_COLUMNS} FROM signals WHERE date = ? ORDER BY rel_vol_rank, id",
        (day.isoformat(),),
    )
    return [signal_from_row(r) for r in rows]


def signal_from_row(r: sqlite3.Row) -> Signal:
    return Signal(
        id=r["id"],
        date=date.fromisoformat(r["date"]),
        symbol=r["symbol"],
        direction=r["direction"],
        status=r["status"],
        rel_vol=float(r["rel_vol"]),
        rel_vol_rank=int(r["rel_vol_rank"]),
        or_open=Decimal(r["or_open"]),
        or_high=Decimal(r["or_high"]),
        or_low=Decimal(r["or_low"]),
        or_close=Decimal(r["or_close"]),
        or_volume=int(r["or_volume"]),
        atr14=Decimal(r["atr14"]),
        adv14=int(r["adv14"]),
        prev_close=Decimal(r["prev_close"]),
        gap_pct=float(r["gap_pct"]),
        trigger_price=db_to_dec(r["trigger_price"]),
        stop_price=db_to_dec(r["stop_price"]),
        max_chase_price=db_to_dec(r["max_chase_price"]),
        entry_limit=db_to_dec(r["entry_limit"]),
        armed_at=parse_et_iso(r["armed_at"]) if r["armed_at"] else None,
        triggered_at=parse_et_iso(r["triggered_at"]) if r["triggered_at"] else None,
        features=json.loads(r["features_json"] or "{}"),
    )
