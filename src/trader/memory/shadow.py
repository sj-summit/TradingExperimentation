"""Shadow outcomes (S§7.2; DECISIONS D-041): what each signal would have earned, independent of
capacity, cash and the LLM, from SIP 1-minute bars.

The simulation is pure and shared with the backtest:

1. **Trigger minute:** the first bar from ``window_start`` (09:35) up to ``last_entry`` whose high
   reaches the trigger (a short: whose low reaches it).
2. **Entry:** max(trigger, the trigger minute's open) + slippage ($0.01 + 0.02% of the trigger):
   a minute that opened past the trigger fills at its open (D-057). A short sells at
   min(trigger, open) - slippage.
3. **Walk the bars from the trigger minute:**
   - **The trigger minute.** A stop touch exits at stop - $0.01 (short: + $0.01): that bar's open
     came before the entry (D-041). When the minute opened short of the trigger, touched the stop
     and closed on its safe side, bars can't tell whether the stop came before the entry or after
     it. That minute is read from the SIP trade tape: the first print at or past the trigger
     orders it (the entry price stays step 2's), and the trade is stopped if a later print is at
     or past the stop, at min(stop, that print) - $0.01. With no usable tape the stop counts,
     the pessimistic fallback (D-057). ``trigger_resolution`` says which: ``bars``, ``tape`` or
     ``fallback``; ``tape_entry`` keeps that first print, which can lie well past the trigger.
   - From the next bar on, a stop touch exits at min(open, stop) - $0.01 (short: max + $0.01).
   - Otherwise the exit is the open of the flatten minute; if that bar is missing, the next bar's
     open, else the last close before it.
4. **R** = (exit - entry) / (entry - stop) per share (short: signs reversed).
   **MFE/MAE** (review evidence only, never a lesson feature) span every bar from the trigger
   minute through the last bar held, plus the exit price. The trigger bar counts whole, which
   is conservative for MAE, except where the tape ordered it: then only its prints from the
   entry print through the exit, or the minute's end, count (D-057).
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
from trader.core.models import Bar, Signal, SipTrade, Trade
from trader.core.prices import CENT
from trader.data.alpaca_rest import Adjustment, AlpacaError, Feed, Timeframe
from trader.data.conditions import counts_for_price
from trader.memory.journal import Journal, dec_to_db

log = structlog.get_logger(__name__)

HUNDRED = Decimal(100)
SIP_DELAY = timedelta(minutes=16)  # SIP bars on the free plan lag 15 min; the minute must close
RETRY_EVERY = timedelta(seconds=30)
GIVE_UP_AFTER = timedelta(minutes=5)

ShadowExit = Literal["stop", "flatten", "no_trigger", "no_data"]
Resolution = Literal["bars", "tape", "fallback"]
VOID_UPDATES = frozenset({"canceled", "incorrect"})  # Alpaca's u: prints that don't stand
MINUTE = timedelta(minutes=1)


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
    trigger_resolution: Resolution | None = None  # how the trigger minute's stop was judged
    tape_entry: Decimal | None = None  # the tape's first print at or past the trigger (D-057)

    @property
    def triggered(self) -> bool:
        return self.trigger_ts is not None


@dataclass(frozen=True)
class TapeVerdict:
    """What the trigger minute's prints say (D-057)."""

    stopped: bool
    entry_print: Decimal  # the first usable print at or past the trigger
    high: Decimal  # the usable prints' range from that print through the exit, or the
    low: Decimal  # minute's end: the trade's excursion in its trigger minute
    exit_price: Decimal | None = None  # min(stop, the print) - $0.01 (short: max + $0.01)


def simulate_long(
    bars: Sequence[Bar],
    *,
    trigger: Decimal,
    stop: Decimal,
    window_start: datetime,
    last_entry: datetime,
    flatten_at: datetime,
    params: ShadowParams,
    tape: Sequence[Trade] | None = None,
) -> ShadowOutcome:
    """``tape``: the trigger minute's prints, when it is ambiguous and they were read."""
    return _simulate(bars, 1, trigger, stop, window_start, last_entry, flatten_at, params, tape)


def simulate_short(
    bars: Sequence[Bar],
    *,
    trigger: Decimal,
    stop: Decimal,
    window_start: datetime,
    last_entry: datetime,
    flatten_at: datetime,
    params: ShadowParams,
    tape: Sequence[Trade] | None = None,
) -> ShadowOutcome:
    return _simulate(bars, -1, trigger, stop, window_start, last_entry, flatten_at, params, tape)


def slippage(trigger: Decimal, params: ShadowParams) -> Decimal:
    return params.slippage_fixed + trigger * params.slippage_pct / HUNDRED


def trigger_minute(
    bars: Sequence[Bar],
    *,
    sign: int,
    trigger: Decimal,
    window_start: datetime,
    last_entry: datetime,
) -> Bar | None:
    """The first bar from ``window_start`` up to ``last_entry`` whose extreme in the trade's
    direction reaches the trigger."""
    for bar in sorted(bars, key=lambda b: b.ts):
        reaches = bar.high >= trigger if sign > 0 else bar.low <= trigger
        if window_start <= bar.ts < last_entry and reaches:
            return bar
    return None


def ambiguous(bar: Bar, *, sign: int, trigger: Decimal, stop: Decimal) -> bool:
    """Whether a trigger minute's bar can't order the entry and a stop touch (D-057): it
    opened short of the trigger, touched the stop and closed on the safe side of it. Opened
    past the trigger, the entry came first; closed past the stop, the touch came last."""
    if sign > 0:
        return bar.open < trigger and bar.low <= stop < bar.close
    return bar.open > trigger and bar.high >= stop > bar.close


def resolve_trigger_minute(
    prints: Sequence[Trade], *, sign: int, trigger: Decimal, stop: Decimal
) -> TapeVerdict | None:
    """The trigger minute from its prints (D-057). Void prints (Alpaca's ``u``) and those
    whose conditions don't set a price (D-043) are left out. The entry is the first print at
    or past the trigger; the trade is stopped if a later one is at or past the stop. None when
    no print reaches the trigger: the tape can't tell."""
    usable = sorted(
        (
            p
            for p in prints
            if counts_for_price(p) and getattr(p, "update", None) not in VOID_UPDATES
        ),
        key=lambda p: p.ts,  # stable: prints at one instant keep the tape's order
    )
    entered = next((i for i, p in enumerate(usable) if (p.price - trigger) * sign >= 0), None)
    if entered is None:
        return None
    held = usable[entered:]
    for k, p in enumerate(held[1:], start=1):
        if (p.price - stop) * sign <= 0:
            seen = [q.price for q in held[: k + 1]]
            worst = min(stop, p.price) if sign > 0 else max(stop, p.price)
            return TapeVerdict(stopped=True, entry_print=held[0].price, high=max(seen),
                               low=min(seen), exit_price=worst - sign * CENT)  # fmt: skip
    seen = [q.price for q in held]
    return TapeVerdict(stopped=False, entry_print=held[0].price, high=max(seen), low=min(seen))


def _simulate(
    bars: Sequence[Bar],
    sign: int,
    trigger: Decimal,
    stop: Decimal,
    window_start: datetime,
    last_entry: datetime,
    flatten_at: datetime,
    params: ShadowParams,
    tape: Sequence[Trade] | None,
) -> ShadowOutcome:
    """``sign`` is +1 for a long and -1 for a short; every price comparison is mirrored by it."""
    direction: Literal["long", "short"] = "long" if sign > 0 else "short"
    path = sorted(bars, key=lambda b: b.ts)
    if not path:
        return ShadowOutcome(direction=direction, exit_reason="no_data")

    def stopped(bar: Bar) -> bool:
        return bar.low <= stop if sign > 0 else bar.high >= stop

    first = trigger_minute(
        path, sign=sign, trigger=trigger, window_start=window_start, last_entry=last_entry
    )
    if first is None:
        return ShadowOutcome(direction=direction, exit_reason="no_trigger")
    start = path.index(first)
    base = max(trigger, first.open) if sign > 0 else min(trigger, first.open)
    entry = base + sign * slippage(trigger, params)
    risk = (entry - stop) * sign
    tick = sign * CENT
    exit_price: Decimal | None = None
    exit_index = start
    reason: ShadowExit = "flatten"
    resolution: Resolution = "bars"
    verdict: TapeVerdict | None = None
    for i in range(start, len(path)):
        bar = path[i]
        if bar.ts >= flatten_at:
            break
        if i == start and ambiguous(bar, sign=sign, trigger=trigger, stop=stop):
            prints = [p for p in tape or () if bar.ts <= p.ts < bar.ts + MINUTE]
            verdict = (
                None
                if tape is None
                else resolve_trigger_minute(prints, sign=sign, trigger=trigger, stop=stop)
            )
            if verdict is None:  # no usable tape: the stop counts (D-041's pessimism)
                resolution = "fallback"
                exit_price, exit_index, reason = stop - tick, i, "stop"
                break
            resolution = "tape"
            if verdict.stopped:
                exit_price, exit_index, reason = verdict.exit_price, i, "stop"
                break
            exit_index = i
            continue
        if stopped(bar):
            # In the trigger minute the bar's open came before the entry (D-041).
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
    spans = [(b.high, b.low) for b in held]
    if verdict is not None:  # the tape ordered the trigger minute: only its held prints count
        spans[0] = (verdict.high, verdict.low)
    favorable = max(high if sign > 0 else -low for high, low in spans)
    adverse = min(low if sign > 0 else -high for high, low in spans)
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
        trigger_resolution=resolution,
        tape_entry=None if verdict is None else verdict.entry_print,
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
    async def trades(
        self, symbols: Sequence[str], start: datetime, end: datetime, *, feed: Feed
    ) -> dict[str, list[SipTrade]]: ...


@dataclass(frozen=True)
class ShadowRun:
    ok: bool
    outcomes: dict[str, ShadowOutcome]  # signal id -> outcome
    error: str = ""
    tape_unresolved: tuple[str, ...] = ()  # "SYM: why" for each ambiguous minute that fell back


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
    tape: bool = False,
) -> ShadowRun:
    """Shadow outcomes for the day's priced signals (longs and shadow shorts) from SIP 1-minute
    bars, written to ``shadow_outcomes`` (idempotent).

    It first waits until the flatten minute's SIP bar can exist (flatten + 16 min), then
    retries every 30 s, for up to 5 min, while Alpaca still refuses the data as too recent.
    Every wait runs on the injected clock. With ``tape``, each ambiguous trigger minute is read
    from the SIP trade tape, one request each (D-057); one that can't be read, or reads no
    print at the trigger, falls back to the pessimistic rule and is listed in the result."""
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
    unresolved: list[str] = []
    for sig in priced:
        trigger, stop = sig.trigger_price, sig.stop_price
        assert trigger is not None
        assert stop is not None
        sign = 1 if sig.direction == "long" else -1
        path = bars.get(sig.symbol, [])
        prints: list[SipTrade] | None = None
        minute = trigger_minute(path, sign=sign, trigger=trigger, window_start=window_start,
                                last_entry=times.last_entry)  # fmt: skip
        if tape and minute is not None and ambiguous(minute, sign=sign, trigger=trigger, stop=stop):
            try:
                found = await market.trades([sig.symbol], minute.ts, minute.ts + MINUTE, feed="sip")
                prints = found.get(sig.symbol, [])
            except AlpacaError as exc:
                log.warning("trigger minute tape unavailable", symbol=sig.symbol, error=str(exc))
                unresolved.append(f"{sig.symbol}: {exc}")
        simulate = simulate_long if sign > 0 else simulate_short
        outcome = simulate(
            path,
            trigger=trigger,
            stop=stop,
            window_start=window_start,
            last_entry=times.last_entry,
            flatten_at=times.flatten,
            params=params,
            tape=prints,
        )
        if prints is not None and outcome.trigger_resolution == "fallback":
            unresolved.append(f"{sig.symbol}: no print at the trigger in its minute")
        outcomes[sig.id] = outcome
    now = clock.now()
    rows = [(sig, outcomes[sig.id]) for sig in priced]
    await journal.write(lambda conn: _store(conn, rows, now))
    log.info(
        "shadow outcomes",
        day=day.isoformat(),
        signals=len(rows),
        triggered=sum(o.triggered for o in outcomes.values()),
        from_tape=sum(o.trigger_resolution == "tape" for o in outcomes.values()),
        fallback=sum(o.trigger_resolution == "fallback" for o in outcomes.values()),
    )
    return ShadowRun(ok=True, outcomes=outcomes, tape_unresolved=tuple(unresolved))


def _store(
    conn: sqlite3.Connection, rows: Sequence[tuple[Signal, ShadowOutcome]], now: datetime
) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO shadow_outcomes (signal_id, entry_price, exit_price, exit_reason, "
        "shadow_r, mfe_r, mae_r, minutes_held, path_source, computed_at, trigger_ts, "
        "sip_only_trigger, trigger_resolution) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sip_1min', ?, ?, ?, ?)",
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
                o.trigger_resolution,
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
