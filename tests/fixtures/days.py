"""A synthetic trading day for orchestrator, replay and golden tests (P3.8a; DECISIONS D-047).

Each symbol's day is a minute path built from keyframes: every minute closes on the straight
line between its keyframes (rounded to the cent) and opens at the previous close, unless a gap
says otherwise. Everything the engine can observe comes from those paths, so they always agree:

- ``SyntheticMarket`` answers the REST calls a day makes: the calendar, Alpaca's clock, assets,
  the scan's 5-minute bars, the readiness poll's 1-minute bar, SIP 1-minute bars for shadow
  outcomes (15 minutes delayed, as on the free plan), SPY's daily history for the regime, and
  snapshots as of the current simulated time.
- ``SyntheticDay.events()`` is the IEX stream: a quote then a trade at each minute's open and
  close (and its extremes), received 20 ms later; flat minutes stream only every tenth minute,
  as a thin feed would. ``ReplayHub`` plays them.
- ``SyntheticDay.universe_rows()`` is the day's eligible universe.
"""

import bisect
import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from tests.fixtures import nyse
from trader.core.clock import ET, Clock
from trader.core.models import Asset, Bar, Quote, SipTrade, Snapshot, Split, Trade
from trader.data.alpaca_rest import Adjustment, Feed, Timeframe
from trader.data.replay import RecordedEvent

D = Decimal
CENT = D("0.01")
OPEN, CLOSE = time(9, 30), time(16, 0)
RX_DELAY = timedelta(milliseconds=20)
IEX_PUBLISH = timedelta(seconds=1)  # a minute bar appears on IEX about a second after it ends
SIP_DELAY = timedelta(minutes=15)  # the free plan's SIP delay


def _t(text: str) -> time:
    return time.fromisoformat(text)


def _minutes(start: time, end: time) -> list[time]:
    """Every minute start in [start, end)."""
    out, m = [], start.hour * 60 + start.minute
    stop = end.hour * 60 + end.minute
    while m < stop:
        out.append(time(m // 60, m % 60))
        m += 1
    return out


@dataclass(frozen=True)
class MinuteBar:
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741 - the bar's low
    c: Decimal
    v: int

    @property
    def flat(self) -> bool:
        return self.o == self.h == self.l == self.c


@dataclass
class SymbolDay:
    symbol: str
    prev_close: Decimal
    atr14: Decimal
    bars: dict[time, MinuteBar]
    adv14: int = 2_000_000
    or_vol_avg14: float = 20_000.0
    exchange: str = "NASDAQ"
    dense: bool = False  # stream every minute three times (the regime symbol)
    in_universe: bool = True


def path(
    symbol: str,
    *,
    open_: str,
    keys: Sequence[tuple[str, str]],
    prev_close: str | None = None,
    atr14: str = "1.00",
    or_volume_per_min: int = 8_000,
    volume: int = 2_000,
    gaps: Mapping[str, str] | None = None,
    end: time = CLOSE,
    dense: bool = False,
    in_universe: bool = True,
) -> SymbolDay:
    """A symbol's minute path. ``keys`` are (HH:MM, close) keyframes from 09:30 on; after the
    last one the price stays flat until ``end``. ``gaps`` make a minute open at a given price."""
    frames = [(_t(t), D(p)) for t, p in keys]
    if frames[0][0] != OPEN:
        raise ValueError("the first keyframe must be 09:30")
    gap = {_t(t): D(p) for t, p in (gaps or {}).items()}
    bars: dict[time, MinuteBar] = {}
    prior = D(open_)
    for minute in _minutes(OPEN, end):
        close = _price_at(frames, minute)
        o = gap.get(minute, prior)
        v = or_volume_per_min if minute < time(9, 35) else (volume if close != o else 500)
        bars[minute] = MinuteBar(o=o, h=max(o, close), l=min(o, close), c=close, v=v)
        prior = close
    return SymbolDay(
        symbol=symbol,
        prev_close=D(prev_close or open_),
        atr14=D(atr14),
        bars=bars,
        dense=dense,
        in_universe=in_universe,
    )


def _price_at(frames: Sequence[tuple[time, Decimal]], minute: time) -> Decimal:
    m = minute.hour * 60 + minute.minute
    for (t0, p0), (t1, p1) in itertools.pairwise(frames):
        a, b = t0.hour * 60 + t0.minute, t1.hour * 60 + t1.minute
        if a <= m <= b:
            return (p0 + (p1 - p0) * (m - a) / (b - a)).quantize(CENT, ROUND_HALF_UP)
    return frames[-1][1] if m > frames[-1][0].hour * 60 + frames[-1][0].minute else frames[0][1]


@dataclass
class SyntheticDay:
    day: date
    symbols: list[SymbolDay]
    splits: list[Split] = field(default_factory=list)
    spy_history_start: date = date(2025, 9, 1)

    def by_symbol(self) -> dict[str, SymbolDay]:
        return {s.symbol: s for s in self.symbols}

    def minute_bars(self, symbol: str) -> list[Bar]:
        found = self.by_symbol().get(symbol)
        if found is None:
            return []
        return [
            Bar(
                ts=datetime.combine(self.day, m, ET),
                open=b.o,
                high=b.h,
                low=b.l,
                close=b.c,
                volume=b.v,
            )
            for m, b in sorted(found.bars.items())
        ]

    def events(self) -> list[RecordedEvent]:
        out: list[RecordedEvent] = []
        for s in self.symbols:
            prior: Decimal | None = None
            for i, (minute, b) in enumerate(sorted(s.bars.items())):
                quiet = b.flat and b.o == prior  # nothing new: a thin feed prints rarely
                prior = b.c
                if quiet and not s.dense and i % 10:
                    continue
                start = datetime.combine(self.day, minute, ET)
                ticks = [(1, b.o)]
                if s.dense:
                    ticks.append((25, (b.o + b.c) / 2))
                up = b.c >= b.o
                for extreme, offset in ((b.l, 20), (b.h, 30)) if up else ((b.h, 20), (b.l, 30)):
                    if extreme not in (b.o, b.c):
                        ticks.append((offset, extreme))
                ticks.append((50, b.c))
                for second, price in sorted(ticks):
                    out.extend(_tick(s.symbol, start + timedelta(seconds=second), price))
        return sorted(out, key=lambda r: r.rx)

    def universe_rows(self) -> list[tuple[Any, ...]]:
        return [
            (
                self.day.isoformat(),
                s.symbol,
                s.symbol,
                s.exchange,
                str(s.prev_close),
                str(s.atr14),
                s.adv14,
                s.or_vol_avg14,
                1,
                None,
            )
            for s in sorted(self.symbols, key=lambda s: s.symbol)
            if s.symbol != "SPY" and s.in_universe
        ]

    def spy_daily(self) -> list[Bar]:
        """A gentle uptrend through the day before, enough for SMA20 and a year of ATR%."""
        sessions = [r["date"] for r in nyse.calendar_rows(self.spy_history_start, self.day)]
        bars: list[Bar] = []
        close = D("520.00")
        for i, d in enumerate(s for s in sessions if s < self.day.isoformat()):
            o = close
            close = (D("520.00") + D("0.25") * i + D(i % 5) * D("0.40")).quantize(CENT)
            hi, lo = max(o, close) + D("2.10"), min(o, close) - D("2.30")
            bars.append(
                Bar(
                    ts=datetime.combine(date.fromisoformat(d), time(0), ET),
                    open=o,
                    high=hi,
                    low=lo,
                    close=close,
                    volume=50_000_000,
                )
            )
        return bars


def _tick(symbol: str, ts: datetime, price: Decimal) -> list[RecordedEvent]:
    price = price.quantize(CENT, ROUND_HALF_UP)
    quote = Quote(symbol=symbol, ts=ts, bid=price - CENT, bid_size=3, ask=price + CENT, ask_size=3)
    trade = Trade(symbol=symbol, ts=ts, price=price, size=100, conditions=("@",))
    return [
        RecordedEvent(rx=ts + RX_DELAY, event=quote),
        RecordedEvent(rx=ts + RX_DELAY, event=trade),
    ]


class SyntheticMarket:
    """The REST market data of a synthetic day (``DayMarket``)."""

    def __init__(self, day: SyntheticDay, clock: Clock, *, skew: timedelta = timedelta(0)) -> None:
        self.day, self._clock, self._skew = day, clock, skew
        self.calls: list[str] = []
        self.fail: dict[str, Exception] = {}  # method -> raised on its next call
        self.delays: dict[str, timedelta] = {}  # method -> how long each call takes
        self._events = day.events()
        self._rx = [e.rx for e in self._events]

    async def _call(self, method: str) -> None:
        self.calls.append(method)
        delay = self.delays.get(method)
        if delay is not None:
            await self._clock.sleep_until(self._clock.now() + delay)
        error = self.fail.pop(method, None)
        if error is not None:
            raise error

    async def calendar(self, start: date, end: date) -> list[dict[str, str]]:
        await self._call("calendar")
        return nyse.calendar_rows(max(start, nyse.FIRST), min(end, nyse.LAST))

    async def clock(self) -> datetime:
        await self._call("clock")
        return self._clock.now() + self._skew

    async def assets(self, *, status: str | None = "active") -> list[Asset]:
        await self._call("assets")
        return [
            Asset(
                symbol=s.symbol,
                name=s.symbol,
                exchange=s.exchange,
                asset_class="us_equity",
                status="active",
                tradable=True,
            )
            for s in self.day.symbols
        ]

    async def asset(self, symbol: str) -> Asset | None:
        return next((a for a in await self.assets() if a.symbol == symbol), None)

    async def bars(
        self,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
        *,
        feed: Feed,
        adjustment: Adjustment = "raw",
    ) -> dict[str, list[Bar]]:
        await self._call(f"bars:{timeframe}")
        now = self._clock.now()
        out: dict[str, list[Bar]] = {}
        for symbol in symbols:
            if timeframe == "1Day":
                found = (
                    [b for b in self.day.spy_daily() if b.ts >= start] if symbol == "SPY" else []
                )
            elif timeframe == "5Min":
                found = self._five(symbol, start, end, now)
            else:
                lag = SIP_DELAY if feed == "sip" else timedelta(0)
                found = [
                    b
                    for b in self.day.minute_bars(symbol)
                    if b.ts >= start
                    and (end is None or b.ts <= end)
                    and b.ts + timedelta(minutes=1) + IEX_PUBLISH + lag <= now
                ]
            if found:
                out[symbol] = found
        return out

    def _five(self, symbol: str, start: datetime, end: datetime | None, now: datetime) -> list[Bar]:
        minutes = [
            b
            for b in self.day.minute_bars(symbol)
            if b.ts >= start and (end is None or b.ts <= end)
        ]
        out: list[Bar] = []
        for i in range(0, len(minutes), 5):
            group = minutes[i : i + 5]
            if len(group) < 5 or group[-1].ts + timedelta(minutes=1) > now:
                continue
            out.append(
                Bar(
                    ts=group[0].ts,
                    open=group[0].open,
                    high=max(b.high for b in group),
                    low=min(b.low for b in group),
                    close=group[-1].close,
                    volume=sum(b.volume for b in group),
                )
            )
        return out

    async def snapshots(self, symbols: Sequence[str], *, feed: Feed) -> dict[str, Snapshot]:
        await self._call("snapshots")
        upto = bisect.bisect_right(self._rx, self._clock.now())
        latest: dict[str, dict[str, Any]] = {}
        for rec in self._events[:upto]:
            ev = rec.event
            kind = "trade" if isinstance(ev, Trade) else "quote"
            latest.setdefault(ev.symbol, {})[kind] = ev
        return {
            s: Snapshot(
                symbol=s, latest_trade=latest[s].get("trade"), latest_quote=latest[s].get("quote")
            )
            for s in symbols
            if s in latest
        }

    async def splits(self, start: date, end: date) -> list[Split]:
        await self._call("splits")
        return [s for s in self.day.splits if start <= s.ex_date <= end]

    async def trades(
        self, symbols: Sequence[str], start: datetime, end: datetime, *, feed: Feed
    ) -> dict[str, list[SipTrade]]:
        """The day's own prints as its SIP tape (D-057)."""
        await self._call("trades")
        out: dict[str, list[SipTrade]] = {}
        for rec in self._events:
            ev = rec.event
            if isinstance(ev, Trade) and ev.symbol in symbols and start <= ev.ts <= end:
                out.setdefault(ev.symbol, []).append(
                    SipTrade(
                        symbol=ev.symbol,
                        ts=ev.ts,
                        price=ev.price,
                        size=ev.size,
                        conditions=ev.conditions,
                    )
                )
        return out


# ---------------------------------------------------------------- the standard test day


DAY = date(2026, 10, 13)  # a Tuesday


def standard_day(day: date = DAY) -> SyntheticDay:
    """Eight signals and SPY. With Book A's $1,000 (S§3.5):

    | rank | symbol | what happens |
    |---|---|---|
    | 1 | WIN | triggers 09:35:50 at 20.41, 24 shares at 20.43, held to the flatten at 21.98 |
    | 2 | LOSE | triggers 09:41:50 at 30.51, 16 shares at 30.53, stopped 10:14:50 at 30.39 |
    | 3 | CHASE | gaps to 41.00 at 09:35: past the 40.53 chase cap when armed: missed_at_arm |
    | 4 | GAPPER | first trades at the trigger at 51.00 (cap 50.56) at 10:15:50: chased |
    | 5 | EXP | never reaches 60.41: expired at 15:30 |
    | 6 | SHORT | the opening range falls: a shadow short, never armed |
    | 7 | DOJI | opens and closes the range at 15.00: no direction |
    | 8 | CAP | triggers 11:00:50, after the day's two entries: the gate's R-007, capacity_entries |
    """
    return SyntheticDay(
        day=day,
        symbols=[
            path(
                "SPY",
                open_="570.00",
                keys=[("09:30", "570.10"), ("16:00", "571.30")],
                or_volume_per_min=900_000,
                volume=400_000,
                dense=True,
            ),
            path(
                "WIN",
                open_="20.00",
                prev_close="19.80",
                or_volume_per_min=16_000,
                keys=[
                    ("09:30", "20.10"),
                    ("09:34", "20.40"),
                    ("09:35", "20.41"),
                    ("12:00", "22.00"),
                ],
            ),
            path(
                "LOSE",
                open_="30.00",
                prev_close="29.70",
                or_volume_per_min=14_000,
                keys=[
                    ("09:30", "30.10"),
                    ("09:34", "30.50"),
                    ("09:40", "30.45"),
                    ("09:41", "30.51"),
                    ("09:50", "30.80"),
                    ("10:20", "30.30"),
                ],
            ),
            path(
                "CHASE",
                open_="40.00",
                prev_close="39.50",
                or_volume_per_min=12_000,
                keys=[("09:30", "40.10"), ("09:34", "40.40"), ("09:35", "41.00")],
                gaps={"09:35": "41.00"},
            ),
            path(
                "GAPPER",
                open_="50.00",
                prev_close="49.40",
                or_volume_per_min=11_000,
                keys=[
                    ("09:30", "50.10"),
                    ("09:34", "50.40"),
                    ("10:14", "50.30"),
                    ("10:15", "51.00"),
                ],
            ),
            path(
                "EXP",
                open_="60.00",
                prev_close="59.20",
                or_volume_per_min=10_000,
                keys=[("09:30", "60.10"), ("09:34", "60.40"), ("10:00", "60.00")],
            ),
            path(
                "SHORT",
                open_="25.00",
                prev_close="25.40",
                or_volume_per_min=9_000,
                keys=[("09:30", "24.90"), ("09:34", "24.60"), ("10:00", "24.00")],
            ),
            path(
                "DOJI",
                open_="15.00",
                prev_close="14.90",
                or_volume_per_min=8_000,
                keys=[("09:30", "15.20"), ("09:32", "14.90"), ("09:34", "15.00")],
            ),
            path(
                "CAP",
                open_="35.00",
                prev_close="34.60",
                or_volume_per_min=7_000,
                keys=[
                    ("09:30", "35.10"),
                    ("09:34", "35.40"),
                    ("10:59", "35.30"),
                    ("11:00", "35.41"),
                ],
            ),
        ],
    )


def without(day: SyntheticDay, symbols: Iterable[str]) -> SyntheticDay:
    gone = set(symbols)
    return SyntheticDay(
        day=day.day, symbols=[s for s in day.symbols if s.symbol not in gone], splits=day.splits
    )
