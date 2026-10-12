"""Shadow outcomes (S§7.2; DECISIONS D-041): what each signal would have earned, independent of
capacity, cash and the LLM, from SIP 1-minute bars.

The simulation is pure and shared with the backtest:

1. **Trigger minute:** the first bar from ``window_start`` (09:35) up to ``last_entry`` whose high
   reaches the trigger (a short: whose low reaches it).
2. **Entry:** trigger + slippage ($0.01 + 0.02% of the trigger); a short sells at
   trigger - slippage.
   The backtest can enter at the trigger bar's open instead when it gapped through the trigger.
3. **Walk the bars from the trigger minute:**
   - In the trigger minute a stop touch exits at stop - $0.01 (short: + $0.01). That bar's open
     came before the entry, so S§7.2's min(open, stop) would book a loss worse than -1R when the
     bar opened under the stop and rallied through the trigger (D-041).
   - From the next bar on, a stop touch exits at min(open, stop) - $0.01 (short: max + $0.01).
   - Otherwise the exit is the open of the flatten minute; if that bar is missing, the next bar's
     open, else the last close before it.
4. **R** = (exit - entry) / (entry - stop) per share (short: signs reversed).
   **MFE/MAE** (review evidence only, never a lesson feature) span every bar from the trigger
   minute through the last bar held, plus the exit price. The trigger bar counts whole, which
   is conservative for MAE.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol

import structlog

from trader.core.calendar import DayTimes, TradingCalendar
from trader.core.clock import Clock, fmt_iso
from trader.core.models import Bar, Signal
from trader.core.prices import CENT
from trader.data.alpaca_rest import Adjustment, AlpacaError, Feed, Timeframe
from trader.memory.journal import Journal, dec_to_db

log = structlog.get_logger(__name__)

HUNDRED = Decimal(100)
SIP_DELAY = timedelta(minutes=16)  # SIP bars on the free plan lag 15 min; the minute must close
RETRY_EVERY = timedelta(seconds=30)
GIVE_UP_AFTER = timedelta(minutes=5)

ShadowExit = Literal["stop", "flatten", "no_trigger", "no_data"]
EntryMode = Literal["trigger", "bar_open"]


@dataclass(frozen=True)
class ShadowParams:
    slippage_fixed: Decimal
    slippage_pct: Decimal  # percent of the trigger: 0.02 means 0.02%


@dataclass(frozen=True)
class ShadowOutcome:
    direction: Literal["long", "short"]
    exit_reason: ShadowExit
    trigger_ts: datetime | None = None  # start of the trigger minute
    trigger_bar_open: Decimal | None = None
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None
    exit_ts: datetime | None = None  # start of the exit minute
    shadow_r: float | None = None
    mfe_r: float | None = None
    mae_r: float | None = None
    minutes_held: int | None = None

    @property
    def triggered(self) -> bool:
        return self.trigger_ts is not None


def simulate_long(
    bars: Sequence[Bar],
    *,
    trigger: Decimal,
    stop: Decimal,
    window_start: datetime,
    last_entry: datetime,
    flatten_at: datetime,
    params: ShadowParams,
    entry_mode: EntryMode = "trigger",
) -> ShadowOutcome:
    return _simulate(bars, 1, trigger, stop, window_start, last_entry, flatten_at, params,
                     entry_mode)  # fmt: skip


def simulate_short(
    bars: Sequence[Bar],
    *,
    trigger: Decimal,
    stop: Decimal,
    window_start: datetime,
    last_entry: datetime,
    flatten_at: datetime,
    params: ShadowParams,
    entry_mode: EntryMode = "trigger",
) -> ShadowOutcome:
    return _simulate(bars, -1, trigger, stop, window_start, last_entry, flatten_at, params,
                     entry_mode)  # fmt: skip


def slippage(trigger: Decimal, params: ShadowParams) -> Decimal:
    return params.slippage_fixed + trigger * params.slippage_pct / HUNDRED


def _simulate(
    bars: Sequence[Bar],
    sign: int,
    trigger: Decimal,
    stop: Decimal,
    window_start: datetime,
    last_entry: datetime,
    flatten_at: datetime,
    params: ShadowParams,
    entry_mode: EntryMode,
) -> ShadowOutcome:
    """``sign`` is +1 for a long and -1 for a short; every price comparison is mirrored by it."""
    direction: Literal["long", "short"] = "long" if sign > 0 else "short"
    path = sorted(bars, key=lambda b: b.ts)
    if not path:
        return ShadowOutcome(direction=direction, exit_reason="no_data")

    def reaches(bar: Bar) -> bool:  # the bar's extreme in the trade's direction hits the trigger
        return bar.high >= trigger if sign > 0 else bar.low <= trigger

    def stopped(bar: Bar) -> bool:
        return bar.low <= stop if sign > 0 else bar.high >= stop

    start = next(
        (i for i, b in enumerate(path) if window_start <= b.ts < last_entry and reaches(b)), None
    )
    if start is None:
        return ShadowOutcome(direction=direction, exit_reason="no_trigger")
    first = path[start]
    base = trigger
    if entry_mode == "bar_open" and (first.open - trigger) * sign > 0:
        base = first.open  # gapped through the trigger: the bar opened past it
    entry = base + sign * slippage(trigger, params)
    risk = (entry - stop) * sign
    tick = sign * CENT
    exit_price: Decimal | None = None
    exit_index = start
    reason: ShadowExit = "flatten"
    for i in range(start, len(path)):
        bar = path[i]
        if bar.ts >= flatten_at:
            break
        if stopped(bar):
            worst = (
                stop if i == start else (min(bar.open, stop) if sign > 0 else max(bar.open, stop))
            )
            exit_price, exit_index, reason = worst - tick, i, "stop"
            break
        exit_index = i
    held = path[start : exit_index + 1]
    if exit_price is None:
        later = next((b for b in path[start:] if b.ts >= flatten_at), None)
        if later is not None:
            exit_price, exit_ts = later.open, later.ts
        else:
            exit_price, exit_ts = path[-1].close, path[-1].ts
    else:
        exit_ts = path[exit_index].ts
    favorable = max(b.high if sign > 0 else -b.low for b in held)
    adverse = min(b.low if sign > 0 else -b.high for b in held)
    signed_exit = exit_price * sign
    best = max(favorable, signed_exit)
    worst_seen = min(adverse, signed_exit)
    return ShadowOutcome(
        direction=direction,
        exit_reason=reason,
        trigger_ts=first.ts,
        trigger_bar_open=first.open,
        entry_price=entry,
        exit_price=exit_price,
        exit_ts=exit_ts,
        shadow_r=float((exit_price - entry) * sign / risk) if risk > 0 else None,
        mfe_r=float((best - entry * sign) / risk) if risk > 0 else None,
        mae_r=float((worst_seen - entry * sign) / risk) if risk > 0 else None,
        minutes_held=int((exit_ts - first.ts).total_seconds() // 60),
    )


# ---------------------------------------------------------------- the post-close job (P3.6b)


class ShadowMarket(Protocol):
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


@dataclass(frozen=True)
class ShadowRun:
    ok: bool
    outcomes: dict[str, ShadowOutcome]  # signal id -> outcome
    error: str = ""


async def compute_shadow(
    *,
    journal: Journal,
    market: ShadowMarket,
    clock: Clock,
    calendar: TradingCalendar,
    day: date,
    times: DayTimes,
    or_minutes: int,
    params: ShadowParams,
    signals: Sequence[Signal],
) -> ShadowRun:
    """Shadow outcomes for the day's priced signals (longs and shadow shorts) from SIP 1-minute
    bars, written to ``shadow_outcomes`` (idempotent).

    It first waits until the flatten minute's SIP bar can exist (flatten + 16 min), then
    retries every 30 s, for up to 5 min, while Alpaca still refuses the data as too recent.
    Every wait runs on the injected clock."""
    priced = [s for s in signals if s.trigger_price is not None and s.stop_price is not None]
    if not priced:
        return ShadowRun(ok=True, outcomes={})
    ready = times.flatten + SIP_DELAY
    if clock.now() < ready:
        await clock.sleep_until(ready)
    session = calendar.session(day)
    symbols = sorted({s.symbol for s in priced})
    give_up = clock.now() + GIVE_UP_AFTER
    while True:
        try:
            bars = await market.bars(symbols, "1Min", session.open, feed="sip", adjustment="raw")
            break
        except AlpacaError as exc:
            if clock.now() >= give_up:
                log.warning("shadow outcomes unavailable", day=day.isoformat(), error=str(exc))
                return ShadowRun(ok=False, outcomes={}, error=str(exc))
            await clock.sleep_until(min(clock.now() + RETRY_EVERY, give_up))
    window_start = session.open + timedelta(minutes=or_minutes)
    outcomes: dict[str, ShadowOutcome] = {}
    for sig in priced:
        trigger, stop = sig.trigger_price, sig.stop_price
        assert trigger is not None
        assert stop is not None
        simulate = simulate_long if sig.direction == "long" else simulate_short
        outcomes[sig.id] = simulate(
            bars.get(sig.symbol, []),
            trigger=trigger,
            stop=stop,
            window_start=window_start,
            last_entry=times.last_entry,
            flatten_at=times.flatten,
            params=params,
        )
    now = clock.now()
    rows = [(sig, outcomes[sig.id]) for sig in priced]
    await journal.write(lambda conn: _store(conn, rows, now))
    log.info(
        "shadow outcomes",
        day=day.isoformat(),
        signals=len(rows),
        triggered=sum(o.triggered for o in outcomes.values()),
    )
    return ShadowRun(ok=True, outcomes=outcomes)


def _store(
    conn: sqlite3.Connection, rows: Sequence[tuple[Signal, ShadowOutcome]], now: datetime
) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO shadow_outcomes (signal_id, entry_price, exit_price, exit_reason, "
        "shadow_r, mfe_r, mae_r, minutes_held, path_source, computed_at, trigger_ts, "
        "sip_only_trigger) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sip_1min', ?, ?, ?)",
        [
            (
                sig.id,
                dec_to_db(o.entry_price),
                dec_to_db(o.exit_price),
                o.exit_reason,
                o.shadow_r,
                o.mfe_r,
                o.mae_r,
                o.minutes_held,
                fmt_iso(now),
                fmt_iso(o.trigger_ts) if o.trigger_ts else None,
                _sip_only(sig, o),
            )
            for sig, o in rows
        ],
    )


def _sip_only(sig: Signal, outcome: ShadowOutcome) -> int | None:
    """1 when the SIP bars reached a long's trigger but the live feed never did; shorts are
    never armed, so the flag doesn't apply to them."""
    if sig.direction != "long":
        return None
    return int(outcome.triggered and sig.triggered_at is None)
