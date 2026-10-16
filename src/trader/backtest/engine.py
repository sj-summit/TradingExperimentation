"""The backtest engine (P3.9; DECISIONS D-042): the live rules on historical days.

Each session runs the same code as live, fed by ``BacktestData``'s as-of view:

1. ``UniverseBuilder`` (with inactive assets) decides eligibility from the prior sessions.
2. The 09:30 opening-range bars and ``select()`` pick the top 20.
3. ``simulate_long``/``simulate_short`` give every signal its shadow outcome (S§7.2): entered at
   max(trigger, the trigger minute's open), with an ambiguous trigger minute read from the SIP
   tape cached by ``--tape`` (else counted as stopped, the pessimistic fallback; D-057).
4. Book A's long-only leg trades the triggered longs in trigger-minute order (then rank), sized by
   ``size_position`` and judged by the real ``RiskGate`` against an in-memory settlement ledger.
   It takes each long's shadow outcome as its own, and skips as ``chased`` past the chase cap.

The engine never calls an LLM (D-010).
"""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import structlog

from trader.books.reasons import reason_for_no_size, reason_from_violations
from trader.config import BookName, FeesConfig, RiskConfig, Settings
from trader.core.calendar import DayTimes, TradingCalendar, day_times
from trader.core.ids import client_order_id, is_ticker
from trader.core.models import Asset, OrderIntent, Signal
from trader.data.alpaca_rest import Feed
from trader.data.universe import UniverseBuilder
from trader.memory.shadow import (
    ShadowOutcome,
    ShadowParams,
    ambiguous,
    simulate_long,
    simulate_short,
    trigger_minute,
)
from trader.risk.gate import GateState, RiskGate, capacity_violations
from trader.risk.ledger import (
    LedgerEntry,
    available,
    cash,
    fill_entries,
    start_of_day_cash,
)
from trader.strategy.orb_sip import Candidate, select, selected, to_signal
from trader.strategy.pricing import entry_limit, is_chased
from trader.strategy.sizing import sizing

from .data import BacktestData

log = structlog.get_logger(__name__)

BOOK: BookName = "A"


@dataclass(frozen=True)
class BacktestSettings:
    start: date
    end: date
    feed: Feed
    starting_cash: Decimal = Decimal(1000)
    tape: bool = False  # fetch the SIP tape of ambiguous trigger minutes not yet cached (D-057)


@dataclass(frozen=True)
class TradeResult:
    symbol: str
    qty: int
    entry_ts: datetime
    entry_price: Decimal
    stop_price: Decimal
    exit_ts: datetime
    exit_price: Decimal
    exit_reason: str
    fees: Decimal

    @property
    def pnl(self) -> Decimal:
        """Gross P&L; ``fees`` are the estimated sale fees."""
        return self.qty * (self.exit_price - self.entry_price)

    @property
    def net_pnl(self) -> Decimal:
        return self.pnl - self.fees

    @property
    def r(self) -> float:
        return float((self.exit_price - self.entry_price) / (self.entry_price - self.stop_price))


@dataclass(frozen=True)
class SignalResult:
    signal: Signal
    shadow: ShadowOutcome | None  # None for a doji, which has no prices
    trade: TradeResult | None = None
    skip: str | None = None  # why a triggered long wasn't traded by Book A's leg
    tape_read: bool = False  # its ambiguous trigger minute's tape was read (cache or fetch)


@dataclass(frozen=True)
class DayResult:
    day: date
    eligible: int
    start_equity: Decimal
    end_cash: Decimal
    signals: tuple[SignalResult, ...]

    @property
    def trades(self) -> list[TradeResult]:
        return [r.trade for r in self.signals if r.trade is not None]

    @property
    def triggered(self) -> int:
        return sum(1 for r in self.signals if r.shadow is not None and r.shadow.triggered)


@dataclass(frozen=True)
class BacktestResult:
    settings: BacktestSettings
    days: tuple[DayResult, ...]
    spy_return: float | None
    requests: int
    skips: dict[str, int] = field(default_factory=dict)


@dataclass
class _Open:
    symbol: str
    qty: int
    entry: Decimal
    stop: Decimal
    exit_ts: datetime


class BookLeg:
    """Book A's long-only leg on simulated fills, through the real risk gate."""

    def __init__(
        self,
        *,
        settings: Settings,
        risk: RiskConfig,
        fees: FeesConfig,
        cash: Decimal,
        first_day: date,
    ) -> None:
        self._settings, self._risk, self._fees = settings, risk, fees
        self._gate = RiskGate(risk, settings.strategy.universe)
        self.ledger: list[LedgerEntry] = [
            LedgerEntry(
                book=BOOK,
                kind="deposit",
                amount=cash,
                trade_date=first_day,
                settle_date=first_day,
                ref="seed",
            )
        ]

    def trade_day(
        self,
        day: date,
        times: DayTimes,
        settle: date,
        longs: Sequence[tuple[Signal, ShadowOutcome]],
    ) -> tuple[dict[str, TradeResult], dict[str, str]]:
        """``longs`` are the armed longs with their shadow outcomes (D-057)."""
        equity = start_of_day_cash(self.ledger, day)
        trades: dict[str, TradeResult] = {}
        skips: dict[str, str] = {}
        positions: list[_Open] = []
        entered: set[str] = set()
        order = sorted(
            ((s, o) for s, o in longs if o.triggered and o.trigger_ts is not None),
            key=lambda so: (so[1].trigger_ts, so[0].rel_vol_rank),
        )
        for sig, out in order:
            assert out.trigger_ts is not None
            assert out.entry_price is not None
            assert out.trigger_bar_open is not None
            assert sig.trigger_price is not None
            assert sig.stop_price is not None
            assert sig.max_chase_price is not None
            now = max(out.trigger_ts, times.entry_start)
            for p in [p for p in positions if p.exit_ts <= now]:
                positions.remove(p)
            base = max(sig.trigger_price, out.trigger_bar_open)
            if is_chased(base, sig.max_chase_price):
                skips[sig.id] = "chased"
                continue
            limit = entry_limit(
                sig.trigger_price,
                base,
                entry=self._settings.strategy.entry,
                max_chase=sig.max_chase_price,
            )
            settled = available(self.ledger, day)
            size = sizing(
                limit,
                sig.stop_price,
                equity,
                settled,
                self._risk.max_risk_per_trade_pct,
                self._risk.max_position_pct,
                Decimal(1),
            )
            if size.qty < 1:  # named as Book A names it (D-056)
                capacity = capacity_violations(
                    self._risk, positions=len(positions), entries_today=len(entered)
                )
                skips[sig.id] = reason_for_no_size(capacity, by_cash=size.by_cash)
                continue
            intent = OrderIntent(
                book=BOOK,
                signal_id=sig.id,
                symbol=sig.symbol,
                qty=size.qty,
                entry_limit=limit,
                stop_price=sig.stop_price,
                client_order_id=client_order_id(BOOK, day, sig.symbol, "entry", 1),
                day_size_multiplier=Decimal(1),
            )
            open_risk = sum((p.qty * (p.entry - p.stop) for p in positions), Decimal(0))
            # Only trades that closed before this decision count as realized (R-015).
            realized = sum((t.net_pnl for t in trades.values() if t.exit_ts <= now), Decimal(0))
            marked = equity + realized
            state = GateState(
                mode="paper",
                now=now,
                day=times,
                exposure_symbols=frozenset(p.symbol for p in positions),
                entries_today=len(entered),
                symbols_entered_today=frozenset(entered),
                available_cash=settled,
                equity_sod=equity,
                realized_pnl_today=realized,
                open_risk=open_risk,
                ledger_equity=marked,
                broker_equity=marked,
                asset=Asset(
                    symbol=sig.symbol,
                    name=sig.symbol,
                    exchange="NYSE",
                    asset_class="us_equity",
                    status="active",
                    tradable=True,
                ),
                quote_age_s=0.0,
            )
            check = self._gate.check(intent, state)
            if not check.approved:
                skips[sig.id] = reason_from_violations(check.violations)
                continue
            qty = check.adjusted_qty or size.qty
            assert out.exit_price is not None
            assert out.exit_ts is not None
            fill_id = f"bt-{day:%Y%m%d}-{sig.symbol}"
            self.ledger += fill_entries(
                book=BOOK,
                side="buy",
                symbol=sig.symbol,
                qty=qty,
                price=out.entry_price,
                trade_date=day,
                settle_date=settle,
                fill_id=f"{fill_id}-buy",
                fees=self._fees,
            )
            sale = fill_entries(
                book=BOOK,
                side="sell",
                symbol=sig.symbol,
                qty=qty,
                price=out.exit_price,
                trade_date=day,
                settle_date=settle,
                fill_id=f"{fill_id}-sell",
                fees=self._fees,
            )
            self.ledger += sale
            fee = -sum((e.amount for e in sale if e.kind == "fee"), Decimal(0))
            trade = TradeResult(
                symbol=sig.symbol,
                qty=qty,
                entry_ts=out.trigger_ts,
                entry_price=out.entry_price,
                stop_price=sig.stop_price,
                exit_ts=out.exit_ts,
                exit_price=out.exit_price,
                exit_reason=out.exit_reason,
                fees=fee,
            )
            trades[sig.id] = trade
            entered.add(sig.symbol)
            positions.append(_Open(sig.symbol, qty, out.entry_price, sig.stop_price, out.exit_ts))
        return trades, skips

    def end_cash(self) -> Decimal:
        return cash(self.ledger)


QUIET_FROM, QUIET_UNTIL = time(9, 0), time(16, 30)


def busy_hours(calendar: TradingCalendar, now: datetime) -> bool:
    """09:00-16:30 ET on a trading day: the backtest would share the data key's rate limit with
    the live scan, so it refuses to run (DECISIONS D-042)."""
    return calendar.is_trading_day(now.date()) and QUIET_FROM <= now.time() < QUIET_UNTIL


def eligible_asset(settings: Settings) -> Callable[[Asset], bool]:
    """The asset-level part of S§3.1 (class, exchange, symbol characters, a real ticker)."""
    universe = settings.strategy.universe

    def ok(asset: Asset) -> bool:
        return (
            asset.asset_class == "us_equity"
            and asset.exchange in universe.exchanges
            and not any(ch in asset.symbol for ch in universe.exclude_symbol_chars)
            and is_ticker(asset.symbol)
        )

    return ok


async def run_backtest(
    data: BacktestData,
    *,
    calendar: TradingCalendar,
    settings: Settings,
    risk: RiskConfig,
    cfg: BacktestSettings,
    progress: Callable[[int, int, DayResult], None] | None = None,
) -> BacktestResult:
    sessions = [s.date for s in calendar.sessions_between(cfg.start, cfg.end)]
    if not sessions:
        raise ValueError(f"no sessions between {cfg.start} and {cfg.end}")
    await data.prepare(cfg.start, cfg.end, symbol_ok=eligible_asset(settings))
    strategy = settings.strategy
    params = ShadowParams(strategy.shadow.slippage_fixed, strategy.shadow.slippage_pct)
    book = BookLeg(
        settings=settings,
        risk=risk,
        fees=settings.fees,
        cash=cfg.starting_cash,
        first_day=sessions[0],
    )
    days: list[DayResult] = []
    skips: Counter[str] = Counter()
    for n, day in enumerate(sessions, start=1):
        view = data.at(day)
        builder = UniverseBuilder(
            data=view,
            journal=data.cache,
            calendar=calendar,
            universe=strategy.universe,
            feed=cfg.feed,
            include_inactive=True,
        )
        _, rows = await builder.build(day, persist=False)
        cands = [
            Candidate(
                symbol=r.symbol,
                prev_close=r.prev_close,
                atr14=r.atr14,
                adv14=r.adv14,
                or_vol_avg14=r.or_vol_avg14,
            )
            for r in rows
            if r.eligible
            and r.prev_close is not None
            and r.atr14 is not None
            and r.adv14 is not None
            and r.or_vol_avg14 is not None
        ]
        session = calendar.session(day)
        or_end = session.open + timedelta(minutes=strategy.selection.or_minutes)
        fetched = await view.bars(
            [c.symbol for c in cands],
            "5Min",
            session.open,
            or_end - timedelta(seconds=1),
            feed=cfg.feed,
            adjustment="split",
        )
        bars = {s: b[0] for s, b in fetched.items() if b and b[0].ts == session.open}
        top = selected(select(day, cands, bars, strategy=strategy, feed=cfg.feed))
        signals = [to_signal(r, strategy=strategy, max_chase_pct=risk.max_chase_pct) for r in top]
        times = day_times(
            session,
            entry_window_start=risk.entry_window_start,
            last_entry=settings.schedule.last_entry,
            flatten=settings.schedule.flatten,
        )
        priced = [s for s in signals if s.trigger_price is not None and s.stop_price is not None]
        minute = await data.minute_bars([s.symbol for s in priced], day)
        shadows: dict[str, ShadowOutcome] = {}
        realistic: list[tuple[Signal, ShadowOutcome]] = []
        read: set[str] = set()
        for s in priced:
            assert s.trigger_price is not None
            assert s.stop_price is not None
            sign = 1 if s.direction == "long" else -1
            path = minute.get(s.symbol, [])
            hit = trigger_minute(path, sign=sign, trigger=s.trigger_price, window_start=or_end,
                                 last_entry=times.last_entry)  # fmt: skip
            prints = None
            if hit is not None and ambiguous(
                hit, sign=sign, trigger=s.trigger_price, stop=s.stop_price
            ):
                prints = await data.tape(s.symbol, hit.ts, fetch=cfg.tape)
                if prints is not None:
                    read.add(s.id)
            sim = simulate_long if sign > 0 else simulate_short
            shadows[s.id] = sim(
                path,
                trigger=s.trigger_price,
                stop=s.stop_price,
                window_start=or_end,
                last_entry=times.last_entry,
                flatten_at=times.flatten,
                params=params,
                tape=prints,
            )
            if s.status == "armed":  # Book A's leg: the same outcome, the same resolver
                realistic.append((s, shadows[s.id]))
        start_equity = start_of_day_cash(book.ledger, day)
        trades, day_skips = book.trade_day(day, times, calendar.settlement_date(day), realistic)
        skips.update(day_skips.values())
        result = DayResult(
            day=day,
            eligible=len(cands),
            start_equity=start_equity,
            end_cash=book.end_cash(),
            signals=tuple(
                SignalResult(
                    signal=s,
                    shadow=shadows.get(s.id),
                    trade=trades.get(s.id),
                    skip=day_skips.get(s.id),
                    tape_read=s.id in read,
                )
                for s in signals
            ),
        )
        days.append(result)
        if progress is not None:
            progress(n, len(sessions), result)
    first, last = sessions[0], sessions[-1]
    closes = await data.daily_closes("SPY", calendar.previous_session(first), last)
    spy = None
    if closes:
        ordered = [closes[d] for d in sorted(closes)]
        spy = float(ordered[-1] / ordered[0] - 1) if len(ordered) > 1 else None
    return BacktestResult(
        settings=cfg,
        days=tuple(days),
        spy_return=spy,
        requests=data.requests,
        skips=dict(sorted(skips.items())),
    )
